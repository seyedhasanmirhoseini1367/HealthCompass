"""
Auto-index MedicalRecord objects when they are created or updated.

The thread is started inside transaction.on_commit() so it only fires after
the DB transaction has fully committed — eliminating the race condition where
the thread tried to SELECT the row before it was visible.

Guards:
- Only re-indexes when content fields change (not every save).
- RAG_AUTO_INDEX_SYNC=True (set in test settings) runs indexing synchronously
  so tests are deterministic and can assert on indexed chunks.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import connection, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Fields whose change warrants a re-index. Saves that only touch other fields
# (e.g. is_flagged, reminder timestamps) skip the embedding API entirely.
_CONTENT_FIELDS = frozenset({'title', 'content', 'parsed_data', 'file', 'record_type'})

# A bounded pool rather than a thread per save.
#
# A Kanta XML import creates one MedicalRecord per document inside a loop, and
# every create fired its own thread — a 200-record import meant 200 threads, each
# opening a database connection and calling the embedding API. The pool caps that
# at a handful of concurrent indexers and queues the rest; work is still done, it
# just cannot exhaust connections or file descriptors.
_INDEX_WORKERS = 2
_executor = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_INDEX_WORKERS,
                    thread_name_prefix='rag-index',
                )
    return _executor


def _index_in_background(record_pk: str):
    """
    Index one record, claiming it first so two workers cannot both do it.

    The claim is the important part. Previously this ran unconditionally and
    stamped `indexed_at` on any positive chunk count, which meant:

      * two workers could index the same record and both declare success;
      * a record whose embeddings were REFUSED (no consent) or FAILED (provider
        down) was still recorded as indexed, because chunks are created before
        they are embedded and the count was read as the outcome.

    Now the record is claimed, indexed, and then marked according to what
    actually happened to its chunks. `indexed_at` is set in exactly one place —
    MedicalRecord.mark_indexed — and only from the INDEXING state.
    """
    from apps.medical_records.models import MedicalRecord
    from apps.rag_assistant.services.rag_service import RAGService

    record = None
    try:
        record = MedicalRecord.objects.get(pk=record_pk)

        if not record.claim_for_indexing():
            # Somebody else holds it, or it is already indexed. Losing the race
            # is the normal outcome of two workers seeing the same save, not an
            # error worth reporting.
            logger.debug('Record %s is already claimed or indexed; skipping',
                         record_pk)
            return

        n = RAGService().index_record(record)
        outcome = RAGService().indexing_outcome(record)

        if outcome == MedicalRecord.IndexStatus.INDEXED:
            record.mark_indexed()
            logger.info('Auto-indexed record %s → %d chunks', record_pk, n)
        elif outcome == MedicalRecord.IndexStatus.BLOCKED:
            record.mark_index_blocked('external processing not permitted')
            logger.info('Record %s not indexed — external processing not '
                        'permitted', record_pk)
        else:
            record.mark_index_failed('one or more chunks have no embedding')
            # Patient-impacting: the record is in their list and the assistant
            # cannot find it.
            from healthcompass.observability import Event as OpsEvent, emit as ops_emit
            ops_emit(OpsEvent.INDEXING_FAILED, record_id=record_pk,
                     error_type='EmbeddingIncomplete')

    except Exception as exc:
        logger.error('Auto-index failed for record %s: %s', record_pk, exc)
        if record is not None:
            # Type only — a provider error can quote the document it was given.
            record.mark_index_failed(type(exc).__name__)
        from healthcompass.observability import Event as OpsEvent, emit as ops_emit
        ops_emit(OpsEvent.INDEXING_FAILED, record_id=record_pk,
                 error_type=type(exc).__name__)
    finally:
        # Pool threads are long-lived, so their database connections would be
        # too. Close explicitly or they accumulate and eventually exhaust the
        # server's connection limit.
        try:
            connection.close()
        except Exception:
            pass


@receiver(post_save, sender='medical_records.MedicalRecord')
def on_medical_record_save(sender, instance, created, **kwargs):
    # Skip re-index when only non-content fields were updated.
    update_fields = kwargs.get('update_fields')
    if update_fields is not None and not (_CONTENT_FIELDS & set(update_fields)):
        return

    pk_str = str(instance.pk)

    # The content changed, so whatever is indexed is now out of date. Marking it
    # stale is what makes the record claimable again — without this an edited
    # record would sit at INDEXED, the claim would be refused, and the new
    # content would never be indexed at all.
    #
    # Deliberately does not disturb a record being indexed right now: that run
    # is either already working from the new content or will be superseded by
    # the save that follows it.
    instance.mark_index_stale()

    from django.conf import settings
    if getattr(settings, 'RAG_AUTO_INDEX_SYNC', False):
        # Synchronous path: used in tests so indexing is deterministic.
        _index_in_background(pk_str)
        return

    # Production path: submit only after the transaction commits, so the row is
    # guaranteed to be visible to the worker's own database connection.
    transaction.on_commit(
        lambda: _get_executor().submit(_index_in_background, pk_str)
    )

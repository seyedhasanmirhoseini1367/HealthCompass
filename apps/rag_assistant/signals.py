"""
Auto-index MedicalRecord objects when they are created or updated.

Indexing is a background job on the 'ingest' queue (healthcompass.tasks),
submitted after the transaction commits so the worker can see the row.

Guards:
- Only re-indexes when content fields change (not every save).
- RAG_AUTO_INDEX_SYNC=True (set in test settings) runs indexing synchronously
  so tests are deterministic and can assert on indexed chunks.
"""
import logging

from django.db import connection, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Fields whose change warrants a re-index. Saves that only touch other fields
# (e.g. is_flagged, reminder timestamps) skip the embedding API entirely.
_CONTENT_FIELDS = frozenset({'title', 'content', 'parsed_data', 'file', 'record_type'})

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
        # In a worker process, close the job's connection so long-lived workers
        # do not accumulate them. Never when running inline (tests, no worker):
        # that connection belongs to the caller, and on PostgreSQL closing it
        # breaks the request or test that triggered the indexing.
        from healthcompass.tasks import is_async
        if is_async():
            try:
                connection.close()
            except Exception as exc:
                logger.debug('Closing the worker DB connection failed: %s', type(exc).__name__)


@receiver(post_save, sender='medical_records.MedicalRecord')
def on_medical_record_save(sender, instance, created, **kwargs):
    # Skip re-index when only non-content fields were updated.
    update_fields = kwargs.get('update_fields')
    if update_fields is not None and not (_CONTENT_FIELDS & set(update_fields)):
        return
    # Not yet parsed: the processing job saves the content, which indexes it.
    if getattr(instance, 'processing_status', 'ready') in ('queued', 'processing'):
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

    dispatch_index(pk_str)


def dispatch_index(pk_str: str) -> None:
    """Index one record: inline under the test runner, else after commit."""
    # On the 'ingest' queue after commit (healthcompass.tasks); inline under the
    # test runner. Replaces an in-process thread pool whose queue died with the
    # worker process (audit ARCH-02).
    from healthcompass.tasks import enqueue
    enqueue(_index_in_background, pk_str, queue='ingest')


def reindex_after_consent(user) -> int:
    """Queue indexing for this user's records that were held back (BLOCKED)
    or never finished, now that external processing is permitted. Returns the
    number queued. Only this user's records are touched."""
    from apps.medical_records.models import MedicalRecord

    pks = [str(pk) for pk in MedicalRecord.objects.filter(
        patient=user, index_status__in=MedicalRecord.CLAIMABLE_INDEX_STATES,
    ).values_list('pk', flat=True)]
    for pk in pks:
        dispatch_index(pk)
    return len(pks)


@receiver(post_save, sender='accounts.Consent')
def on_consent_saved(sender, instance, created, **kwargs):
    """Granting external_llm consent makes earlier uploads searchable
    (audit SEC-09): they were saved, but not sent out for embedding."""
    from apps.accounts.models import Consent, ConsentPurpose

    if (created and instance.purpose == ConsentPurpose.EXTERNAL_LLM
            and instance.status == Consent.Status.GRANTED):
        user = instance.user
        transaction.on_commit(lambda: reindex_after_consent(user))

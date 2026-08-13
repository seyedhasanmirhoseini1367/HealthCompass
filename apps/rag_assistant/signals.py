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
    try:
        from apps.medical_records.models import MedicalRecord
        from apps.rag_assistant.services.rag_service import RAGService

        record = MedicalRecord.objects.get(pk=record_pk)
        svc    = RAGService()
        n      = svc.index_record(record)
        logger.info('Auto-indexed record %s → %d chunks', record_pk, n)
    except Exception as exc:
        logger.error('Auto-index failed for record %s: %s', record_pk, exc)
        # Without this the record silently never becomes searchable.
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

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

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Fields whose change warrants a re-index. Saves that only touch other fields
# (e.g. is_flagged, reminder timestamps) skip the embedding API entirely.
_CONTENT_FIELDS = frozenset({'title', 'content', 'parsed_data', 'file', 'record_type'})


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

    # Production path: start the thread only after the transaction commits so
    # the row is guaranteed to be visible to the new thread's DB connection.
    transaction.on_commit(
        lambda: threading.Thread(
            target=_index_in_background,
            args=(pk_str,),
            daemon=True,
        ).start()
    )

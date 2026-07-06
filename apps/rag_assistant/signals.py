# rag_assistant/signals.py
"""
Auto-index MedicalRecord objects when they are created or updated.
The signal runs in the background thread so the HTTP request returns quickly.
"""
import logging
import threading

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _index_in_background(record_pk: str):
    """Run indexing in a daemon thread to avoid blocking the request cycle."""
    try:
        from apps.medical_records.models import MedicalRecord
        from apps.rag_assistant.services.rag_service import RAGService

        record = MedicalRecord.objects.get(pk=record_pk)
        svc    = RAGService()
        n      = svc.index_record(record)
        logger.info('Auto-indexed record %s → %d chunks', record_pk, n)
    except Exception as e:
        logger.exception('Auto-index failed for record %s: %s', record_pk, e)


@receiver(post_save, sender='medical_records.MedicalRecord')
def on_medical_record_save(sender, instance, created, **kwargs):
    """Trigger re-indexing whenever a MedicalRecord is saved."""
    # Use a daemon thread so the web worker is not blocked
    t = threading.Thread(
        target=_index_in_background,
        args=(str(instance.pk),),
        daemon=True,
    )
    t.start()

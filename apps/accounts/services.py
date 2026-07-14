import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def purge_user_data(user) -> None:
    """
    Right-to-erasure: delete all PHI for the user, including physical files.

    Django's CASCADE handles DB rows (medical records, chat sessions, query
    logs, RAG chunks, notifications, appointments, predictions, alert logs).
    We handle the two things CASCADE cannot reach:
      1. Physical files referenced by FileField / ImageField.
      2. The user row itself.

    DoctorAccessLog rows where this user was the patient are NOT deleted —
    they are the audit trail required by Finnish health law. The patient FK
    becomes NULL via on_delete=SET_NULL, anonymising the reference without
    destroying the audit chain.

    Note: FAISS vector-store files on disk are shared across users; individual
    embeddings cannot be selectively removed. The source content (MedicalChunk
    rows) is deleted by CASCADE, leaving the FAISS index stale. A full rebuild
    of the index handles the gap; for a per-user right-to-erasure guarantee,
    migrate to a per-user index or a DB-backed vector store.
    """
    with transaction.atomic():
        # ── 1. Profile picture ────────────────────────────────────────────────
        if user.profile_picture:
            try:
                user.profile_picture.delete(save=False)
            except Exception as exc:
                logger.warning('Could not delete profile picture for user %s: %s', user.pk, exc)

        # ── 2. Medical record files (PDFs, images, etc.) ──────────────────────
        for record in user.medical_records.select_related().all():
            if record.file:
                try:
                    record.file.delete(save=False)
                except Exception as exc:
                    logger.warning('Could not delete file for record %s: %s', record.pk, exc)

        # ── 3. Delete the user — CASCADE handles all linked DB rows ───────────
        user.delete()

import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def erase_uploaded_file(storage, name: str, *, label: str) -> bool:
    """
    Remove one uploaded file from storage. The single authoritative erasure.

    Every path that destroys a row owning a file calls this, so there is one
    place that decides what "the file is gone" means and one place that reports
    when it is not.

    Idempotent by design: deleting a file that is already absent is a success,
    not an error. Django's FileSystemStorage.delete() swallows FileNotFoundError
    itself, and the explicit check here makes the intent legible and covers
    backends that do not. That property is what allows more than one path to
    cover the same file — a record deleted as part of a user erasure is reached
    by both the cascade and the purge, and the second call is a no-op rather
    than a spurious failure.

    Returns True when the file is gone afterwards. Never raises: erasure runs
    after the database work has committed, so raising here would fail an
    operation that has already succeeded.
    """
    if not name:
        return True
    try:
        if storage.exists(name):
            storage.delete(name)
        return True
    except Exception as exc:
        logger.error('Erasure: could not delete %s (%s): %s', name, label, exc)
        return False


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
    from apps.ai_insights.models import AIModel, ModelPrediction
    from healthcompass.observability import Event as OpsEvent, emit as ops_emit

    user_pk = user.pk

    # ── 1. Collect every file this user owns, BEFORE deleting any row ────────
    #
    # Previously only the profile picture and MedicalRecord.file were removed.
    # ModelPrediction.input_file (uploaded EEG/images submitted for inference)
    # and AIModel.model_file for data-scientist accounts were left on disk. The
    # DB rows cascaded away, so those files became unreachable through
    # _user_can_access_media — which checks a row that no longer exists — and
    # could not even be found and removed through the application afterwards.
    # An erasure request that leaves the data present is not fulfilled.
    targets = []
    if user.profile_picture:
        targets.append(('profile_picture', user.profile_picture))
    for record in user.medical_records.all():
        if record.file:
            targets.append((f'record:{record.pk}', record.file))
    for prediction in ModelPrediction.objects.filter(patient=user):
        if prediction.input_file:
            targets.append((f'prediction:{prediction.pk}', prediction.input_file))
    for model in AIModel.objects.filter(data_scientist=user):
        if model.model_file:
            targets.append((f'ai_model:{model.pk}', model.model_file))

    # ── 2. Delete the DB rows first — CASCADE handles everything linked ──────
    with transaction.atomic():
        user.delete()

    # ── 3. Only then remove the files ────────────────────────────────────────
    #
    # Deliberately OUTSIDE the transaction. File deletion has no compensating
    # action: if the transaction rolled back, the rows would return while the
    # bytes were already gone irreversibly. Doing it after the commit means the
    # worst case is an orphaned file, which is recoverable, rather than a record
    # pointing at nothing, which is not.
    # Through the shared primitive, so the rule for "the file is gone" is the
    # same one the record-deletion path uses. MedicalRecord files are also
    # reached by the post_delete receiver as the user cascade commits; that
    # overlap is harmless because erase_uploaded_file is idempotent, and it is
    # kept deliberately so this function still erases everything it collected
    # even if a signal is ever disconnected.
    failed = 0
    for label, field_file in targets:
        if not erase_uploaded_file(field_file.storage, field_file.name, label=label):
            failed += 1

    if failed:
        # A silently skipped file is an unfulfilled erasure request, so this is
        # an operational event rather than a warning line.
        ops_emit(OpsEvent.ERASURE_INCOMPLETE, user_id=user_pk,
                 files_remaining=failed, files_total=len(targets))

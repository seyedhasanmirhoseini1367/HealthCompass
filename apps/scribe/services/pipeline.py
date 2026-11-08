"""
Encounter processing: uploaded → transcribing → generating → done | failed.

Runs in a plain daemon thread (no task queue). Consequences, accepted for an MVP:
  * a process restart kills in-flight jobs — mark_if_stale() turns those into
    'failed' so the page does not poll forever;
  * each web worker process loads its own Whisper model.
"""
import datetime
import logging
import threading

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from .errors import ScribeError

logger = logging.getLogger(__name__)

GENERIC_ERROR = 'Processing failed unexpectedly. Check the server logs for details.'


def _set_status(encounter, status, error=''):
    encounter.status = status
    encounter.error_message = error
    encounter.save(update_fields=['status', 'error_message', 'updated_at'])


def run_pipeline(encounter_id):
    """Process one encounter synchronously. Never raises."""
    from apps.scribe.models import Encounter
    from apps.scribe.services import evidence, note_generation, transcription

    S = Encounter.Status
    try:
        encounter = Encounter.objects.select_related('owner').get(pk=encounter_id)
    except Encounter.DoesNotExist:
        return  # deleted before the job started

    try:
        _set_status(encounter, S.TRANSCRIBING)
        transcript = transcription.transcribe(encounter)

        _set_status(encounter, S.GENERATING)
        note = note_generation.generate_note(transcript)

        try:
            evidence.attach_evidence(note)
        except Exception:
            logger.exception('scribe: attaching evidence failed for encounter %s', encounter_id)

        _set_status(encounter, S.DONE)
    except ScribeError as exc:
        _set_status(encounter, S.FAILED, str(exc))
    except Exception:
        logger.exception('scribe: pipeline crashed for encounter %s', encounter_id)
        try:
            _set_status(encounter, S.FAILED, GENERIC_ERROR)
        except Exception:
            logger.exception('scribe: could not record failure for encounter %s', encounter_id)


def _thread_main(encounter_id):
    close_old_connections()
    try:
        run_pipeline(encounter_id)
    finally:
        # Threads get their own DB connection; Django will not close it for us.
        connection.close()


def start_pipeline(encounter):
    """
    Start processing in a background thread once the upload has committed, so
    the thread can never read the row before it exists.
    """
    pk = encounter.pk
    transaction.on_commit(lambda: threading.Thread(
        target=_thread_main, args=(pk,), name=f'scribe-{pk}', daemon=True).start())


def mark_if_stale(encounter):
    """
    Fail an encounter that has sat in a processing state for too long — almost
    always because the process running its thread was restarted.
    """
    if not encounter.is_in_progress:
        return encounter
    minutes = int(getattr(settings, 'SCRIBE_STALE_MINUTES', 120))
    if encounter.updated_at < timezone.now() - datetime.timedelta(minutes=minutes):
        _set_status(encounter, encounter.Status.FAILED,
                    'Processing was interrupted (the server may have restarted). '
                    'Delete this encounter and upload the recording again.')
    return encounter

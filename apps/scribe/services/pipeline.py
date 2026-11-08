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
import time

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


HEARTBEAT_SECONDS = 30


def _heartbeat(encounter_id):
    """
    Return a callback that bumps updated_at at most every HEARTBEAT_SECONDS.

    A job whose process dies (e.g. killed for running out of memory) stops
    beating, so mark_if_stale() can fail it within minutes instead of leaving
    the page on "Transcribing" indefinitely.
    """
    from apps.scribe.models import Encounter
    last = [0.0]

    def beat():
        now = time.monotonic()
        if now - last[0] >= HEARTBEAT_SECONDS:
            last[0] = now
            Encounter.objects.filter(pk=encounter_id).update(updated_at=timezone.now())
    return beat


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
        transcript = transcription.transcribe(encounter, on_progress=_heartbeat(encounter.pk))

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
    Fail an encounter that has shown no progress for too long — almost always
    because the process running its thread was restarted or killed.

    Transcription beats every HEARTBEAT_SECONDS, so the window only has to
    cover the gaps between beats: the one-off model download and load, and an
    upload queued behind another job.
    """
    if not encounter.is_in_progress:
        return encounter
    minutes = int(getattr(settings, 'SCRIBE_STALE_MINUTES', 15))
    if encounter.updated_at < timezone.now() - datetime.timedelta(minutes=minutes):
        _set_status(encounter, encounter.Status.FAILED,
                    'Processing stopped without finishing — the server was most likely '
                    'restarted, often because it ran out of memory loading the speech '
                    'model. Try again, or use a smaller model (see the scribe README).')
    return encounter


def retry(encounter):
    """Reset a failed encounter and process it again from the start."""
    from apps.scribe.models import Note, Transcript
    Note.objects.filter(encounter=encounter).delete()
    Transcript.objects.filter(encounter=encounter).delete()
    _set_status(encounter, encounter.Status.UPLOADED)
    start_pipeline(encounter)

"""
Local speech-to-text with faster-whisper (CPU, int8).

The model is loaded lazily, once per process, and transcriptions are serialised
with a lock: two concurrent uploads would otherwise double CPU and RAM use on a
small host for no gain in throughput.
"""
import contextlib
import logging
import os
import tempfile
import threading

from django.conf import settings

from .errors import AUDIO_MISSING, ScribeError

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()
_transcribe_lock = threading.Lock()


def _cfg(name, default):
    """Django setting if defined, else environment variable (or .env), else default."""
    if hasattr(settings, name):
        return getattr(settings, name)
    from decouple import config
    return config(name, default=default)


def get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise ScribeError(
                        'Speech-to-text is not installed on this server '
                        '(pip install faster-whisper).') from exc
                size = _cfg('SCRIBE_WHISPER_MODEL', 'small')
                logger.info('scribe: loading faster-whisper model %r (cpu, int8)', size)
                _model = WhisperModel(
                    size, device='cpu', compute_type='int8',
                    cpu_threads=int(_cfg('SCRIBE_WHISPER_THREADS', 2)),
                )
    return _model


def transcribe_audio(audio, on_progress=None):
    """
    Transcribe a file path (or a 16 kHz mono float32 numpy array).

    *on_progress*, if given, is called after each decoded segment — the
    pipeline uses it as a heartbeat so a job killed mid-way is detected quickly.

    Returns {"text", "segments": [{"start", "end", "text"}], "language", "duration"}.
    """
    model = get_model()
    if on_progress:
        on_progress()
    with _transcribe_lock:
        segments_iter, info = model.transcribe(
            audio,
            beam_size=int(_cfg('SCRIBE_WHISPER_BEAM_SIZE', 1)),
            # Skips silence; also cuts down Whisper's habit of hallucinating
            # text over long pauses. Uses the Silero ONNX model bundled with
            # faster-whisper (onnxruntime is already a project dependency).
            vad_filter=True,
        )
        # The iterator is lazy — decoding happens here, inside the lock.
        segments = []
        for seg in segments_iter:
            if on_progress:
                on_progress()
            text = (seg.text or '').strip()
            if text:
                segments.append({'start': round(float(seg.start), 2),
                                 'end':   round(float(seg.end), 2),
                                 'text':  text})
    return {
        'text':     ' '.join(s['text'] for s in segments),
        'segments': segments,
        'language': getattr(info, 'language', '') or '',
        'duration': float(getattr(info, 'duration', 0.0) or 0.0),
    }


@contextlib.contextmanager
def local_audio_path(field_file):
    """
    Yield a local filesystem path for a FileField.

    Local storage exposes .path directly; object storage (S3/R2) does not, so the
    file is copied to a temporary file that is removed afterwards.
    """
    try:
        local = field_file.path
    except NotImplementedError:
        local = None
    if local:
        yield local
        return

    suffix = os.path.splitext(field_file.name)[1]
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix='scribe-')
    try:
        with os.fdopen(fd, 'wb') as out, field_file.open('rb') as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b''):
                out.write(chunk)
        yield tmp_path
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)


def transcribe(encounter, on_progress=None):
    """Transcribe an Encounter's audio and store the Transcript."""
    from apps.scribe.models import Transcript

    if not encounter.audio_available:
        raise ScribeError(AUDIO_MISSING)

    with local_audio_path(encounter.audio) as path:
        try:
            result = transcribe_audio(path, on_progress=on_progress)
        except ScribeError:
            raise
        except Exception as exc:
            logger.exception('scribe: transcription failed for encounter %s', encounter.pk)
            raise ScribeError('Could not decode or transcribe the audio file.') from exc

    if not result['segments']:
        raise ScribeError('No speech was detected in the recording.')

    transcript, _ = Transcript.objects.update_or_create(
        encounter=encounter,
        defaults={
            'text':     result['text'],
            'segments': result['segments'],
            'language': result['language'],
            'duration': result['duration'],
        },
    )
    return transcript

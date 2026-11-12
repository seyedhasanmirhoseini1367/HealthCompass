"""Record that a job ran. Use as a decorator on a management command's handle()
(`@heartbeat('purge_old_query_logs')`) or as a context manager.

Best-effort: a heartbeat write that fails (database down, a test swapping the
engine) is logged and ignored, each write in its own savepoint, so it can never
replace the job's own exception or abort the caller's transaction.
"""
import functools
import logging
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def _record(name, **fields):
    from .models import JobHeartbeat

    try:
        with transaction.atomic():
            JobHeartbeat.objects.update_or_create(name=name, defaults=fields)
    except Exception as exc:
        logger.warning('Could not record heartbeat for %s: %s', name, type(exc).__name__)


@contextmanager
def beat(name: str):
    _record(name, last_started=timezone.now())
    try:
        yield
    except BaseException as exc:
        _record(name, last_error=type(exc).__name__[:200], last_error_at=timezone.now())
        raise
    _record(name, last_succeeded=timezone.now(), last_error='')


def heartbeat(name: str):
    def decorator(handle):
        @functools.wraps(handle)
        def wrapped(*args, **kwargs):
            with beat(name):
                return handle(*args, **kwargs)
        return wrapped
    return decorator

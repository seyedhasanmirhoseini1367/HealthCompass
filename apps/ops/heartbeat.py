"""Record that a job ran. Use as a decorator on a management command's handle()
(`@heartbeat('purge_old_query_logs')`) or as a context manager."""
import functools
from contextlib import contextmanager

from django.utils import timezone


@contextmanager
def beat(name: str):
    from .models import JobHeartbeat

    JobHeartbeat.objects.update_or_create(name=name, defaults={'last_started': timezone.now()})
    try:
        yield
    except BaseException as exc:
        JobHeartbeat.objects.filter(name=name).update(
            last_error=type(exc).__name__[:200], last_error_at=timezone.now())
        raise
    JobHeartbeat.objects.filter(name=name).update(last_succeeded=timezone.now(), last_error='')


def heartbeat(name: str):
    def decorator(handle):
        @functools.wraps(handle)
        def wrapped(*args, **kwargs):
            with beat(name):
                return handle(*args, **kwargs)
        return wrapped
    return decorator

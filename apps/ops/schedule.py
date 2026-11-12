"""
What runs when (audit ARCH-03). One table, read by `run_scheduler`.

Each entry: (job name, management command and arguments, interval, condition).
A job is due when it has not started within its interval, judged from
JobHeartbeat, so restarting the scheduler neither skips nor repeats work.
"""
import shutil
from datetime import timedelta


def _pg_dump_available() -> bool:
    from django.conf import settings
    engine = settings.DATABASES['default'].get('ENGINE', '')
    return 'postgresql' in engine and shutil.which('pg_dump') is not None


SCHEDULE = [
    ('run_care_cycle',             ['run_care_cycle', '--apply'], timedelta(minutes=15), None),
    ('send_appointment_reminders', ['send_appointment_reminders'], timedelta(minutes=5), None),
    ('retry_failed_embeddings',    ['retry_failed_embeddings'],   timedelta(hours=1), None),
    ('reindex_unindexed_records',  ['reindex_unindexed_records'], timedelta(hours=1), None),
    ('purge_old_query_logs',       ['purge_old_query_logs'],      timedelta(days=1), None),
    ('flushexpiredtokens',         ['flushexpiredtokens'],        timedelta(days=1), None),
    ('backup_database',            ['backup_database'],           timedelta(days=1), _pg_dump_available),
]

#: A job is stale when it has not succeeded within this many intervals.
STALE_AFTER_INTERVALS = 3


def run_job(name: str) -> None:
    """Run one scheduled job by name, recording its heartbeat."""
    from django.core.management import call_command

    from .heartbeat import beat

    entry = next(e for e in SCHEDULE if e[0] == name)
    with beat(name):
        call_command(*entry[1])


def due_jobs(now):
    from .models import JobHeartbeat

    beats = {b.name: b for b in JobHeartbeat.objects.all()}
    due = []
    for name, _argv, interval, condition in SCHEDULE:
        if condition is not None and not condition():
            continue
        last = getattr(beats.get(name), 'last_started', None)
        if last is None or now - last >= interval:
            due.append(name)
    return due


def stale_jobs(now):
    """Names of scheduled jobs that have not succeeded recently enough."""
    from .models import JobHeartbeat

    beats = {b.name: b for b in JobHeartbeat.objects.all()}
    stale = []
    for name, _argv, interval, condition in SCHEDULE:
        if condition is not None and not condition():
            continue
        ok = getattr(beats.get(name), 'last_succeeded', None)
        if ok is None or now - ok > interval * STALE_AFTER_INTERVALS:
            stale.append(name)
    return stale

"""
Turning a schedule into occurrences, and closing the ones nobody answered.

Two jobs, deliberately separate:

  `generate_occurrences` creates the rows that will be asked about;
  `sweep_unconfirmed`    records that the grace window closed with no answer.

The second is the one that has to be careful. It is the only place in this
system where the absence of input becomes a stored fact, and the only honest
fact available is "we do not know". It therefore writes UNCONFIRMED and never
MISSED — those are different claims, and only a person can make the second one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: How far ahead occurrences are materialised. Far enough that a patient sees
#: today and tomorrow, short enough that editing a schedule does not leave a
#: month of stale rows behind it.
HORIZON_DAYS = 2


def _parse_time(value: str):
    """"HH:MM" -> time, or None if the schedule holds something unusable."""
    try:
        hour, minute = str(value).strip().split(':')
        parsed = datetime.strptime(f'{int(hour):02d}:{int(minute):02d}', '%H:%M').time()
    except (ValueError, AttributeError, TypeError):
        return None
    return parsed


def _due_times(task, *, since, until):
    """Every due moment for this task inside the window, in local time."""
    tz = timezone.get_current_timezone()
    times = [t for t in (_parse_time(v) for v in (task.times_of_day or [])) if t]
    if not times:
        return

    day = timezone.localtime(since, tz).date()
    last = timezone.localtime(until, tz).date()
    while day <= last:
        for at in times:
            naive = datetime.combine(day, at)
            # make_aware rather than arithmetic on UTC: "08:00" means eight in
            # the morning where the patient lives, including across a DST shift,
            # and a dose that drifts by an hour twice a year is a real problem.
            try:
                due = timezone.make_aware(naive, tz)
            except Exception:
                # Non-existent or ambiguous local time (the DST gap). Skipping
                # is right: there was no 03:30 that day, so nothing was due.
                logger.info('Skipping impossible local time %s for task %s', naive, task.pk)
                continue
            if since <= due <= until:
                yield due
        day += timedelta(days=1)


def generate_occurrences(*, patient=None, horizon_days: int = HORIZON_DAYS) -> int:
    """
    Materialise upcoming occurrences for active tasks. Idempotent.

    Safe to run repeatedly — the unique constraint on (task, due_at) is what
    makes that true, and it is enforced in the database rather than by checking
    first, because two cron workers can check at the same time and both find
    nothing. A caregiver seeing every dose twice would make a compliant patient
    look erratic, which is worse than the job failing outright.
    """
    from .models import CareTask, TaskOccurrence

    tasks = CareTask.objects.filter(is_active=True)
    if patient is not None:
        tasks = tasks.filter(patient=patient)

    now   = timezone.now()
    until = now + timedelta(days=horizon_days)
    created = 0

    for task in tasks.select_related('patient'):
        for due in _due_times(task, since=now, until=until):
            try:
                with transaction.atomic():
                    TaskOccurrence.objects.create(
                        task=task, patient=task.patient, due_at=due)
                created += 1
            except IntegrityError:
                pass        # already generated — the constraint did its job
    return created


def sweep_unconfirmed(*, patient=None) -> int:
    """
    Close out occurrences whose grace window has passed with no answer.

    What this records is ignorance, not non-adherence. A person who takes their
    tablet and puts the phone down leaves exactly the same trace as a person who
    forgot, so the only supportable statement is that nobody answered.

    Only PENDING rows are touched. If a late-running sweep meets an occurrence
    someone has since confirmed, the person's answer stands — their statement
    about their own behaviour outranks our silence, always.
    """
    from .models import CareTask, TaskOccurrence

    now = timezone.now()
    qs = (TaskOccurrence.objects
          .filter(state=TaskOccurrence.State.PENDING)
          .select_related('task'))
    if patient is not None:
        qs = qs.filter(patient=patient)

    # Grace is per task, so the deadline is computed per row rather than in one
    # blanket filter. The candidate set is bounded by the largest grace in use,
    # which keeps this from scanning every occurrence ever created.
    longest = (CareTask.objects.order_by('-grace_minutes')
               .values_list('grace_minutes', flat=True).first() or 0)
    qs = qs.filter(due_at__lte=now - timedelta(minutes=0),
                   due_at__gte=now - timedelta(minutes=longest) - timedelta(days=7))

    swept = 0
    for occurrence in qs:
        deadline = occurrence.due_at + timedelta(minutes=occurrence.task.grace_minutes)
        if now > deadline and occurrence.mark_unconfirmed():
            swept += 1
    return swept

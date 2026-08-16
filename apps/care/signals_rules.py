"""
Turning events into signals — the first place the system asserts anything.

Named `signals_rules` rather than `signals` so it is never confused with Django
signal receivers; this module is about monitoring, not about post_save.

Every rule here counts. None of them interprets. "No answer three times" is a
statement about the application's records; "your mother is not taking her
medication" is a statement about a person, and this layer is not entitled to
make the second one from the evidence available to it.

Each signal carries the exact rows the rule read, so the question "which three?"
has an answer, and so a signal whose evidence is deleted loses its justification
along with it.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .policy import policy

logger = logging.getLogger(__name__)


def _recent_duplicate(patient, kind, subject_key, *, cooldown_hours):
    """
    An open signal for the same thing, or one raised inside the cooldown.

    This is the whole defence against alert fatigue. A task left unanswered for
    a week would otherwise raise a fresh caregiver signal every single day until
    the caregiver stops reading them — at which point the feature has become
    worse than not having it, because it looks like it is working.
    """
    from django.db.models import Q

    from .models import MonitoringSignal

    since = timezone.now() - timedelta(hours=cooldown_hours)
    return (MonitoringSignal.objects
            .filter(patient=patient, kind=kind, subject_key=subject_key)
            # Still open, OR closed but raised recently. The second half matters:
            # a signal that resolved this morning and would re-raise this
            # afternoon is the same worry twice in one day.
            .filter(Q(resolved_at__isnull=True) | Q(created_at__gte=since))
            .order_by('-created_at')
            .first())


def _trailing_unconfirmed_streak(task):
    """
    How many of the most recent DUE occurrences in a row went unanswered.

    Walks backwards from the newest and stops at the first answered one, so a
    single confirmation genuinely breaks the streak rather than being averaged
    away. Occurrences still inside their grace window are skipped rather than
    counted: they are not yet evidence of anything, and counting them would
    raise a signal before the patient has had a chance to respond.
    """
    from .models import TaskOccurrence

    streak = []
    recent = (task.occurrences
              .filter(due_at__lte=timezone.now())
              .order_by('-due_at')[:20])
    for occurrence in recent:
        if occurrence.state == TaskOccurrence.State.PENDING:
            continue
        if occurrence.state != TaskOccurrence.State.UNCONFIRMED:
            break
        streak.append(occurrence)
    return streak


@transaction.atomic
def evaluate_task(task) -> list:
    """
    Look at one task and raise what it warrants. Returns new signals.

    Two thresholds, both product settings rather than clinical ones: the patient
    is reminded first because they are the person who can actually resolve it,
    and a caregiver is only involved once that has repeatedly failed.
    """
    from .models import MonitoringSignal

    config = policy()
    streak = _trailing_unconfirmed_streak(task)
    if not streak:
        _resolve_open(task)
        return []

    if len(streak) < config.unconfirmed_streak_for_caregiver:
        # Below the caregiver threshold. The patient-facing reminder is a
        # notification concern, not a signal — nothing has been established yet
        # that anyone else needs to know about.
        return []

    subject_key = f'task:{task.pk}'
    if _recent_duplicate(task.patient, MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
                         subject_key, cooldown_hours=config.resignal_cooldown_hours):
        return []

    signal = MonitoringSignal.objects.create(
        patient      = task.patient,
        kind         = MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
        severity     = MonitoringSignal.Severity.ATTENTION,
        window_start = streak[-1].due_at,
        window_end   = streak[0].due_at,
        subject_key  = subject_key,
        rule         = 'unconfirmed_streak',
    )
    signal.occurrences.set(streak)
    logger.info('Raised %s for task %s over %d occurrence(s)',
                signal.kind, task.pk, len(streak))
    return [signal]


def _resolve_open(task):
    """
    The situation stopped being true, so say so rather than deleting.

    A signal that was raised and then resolved is worth keeping: it is the
    record of a worry that turned out fine, and a caregiver who was notified
    deserves to see it closed rather than have it vanish.
    """
    from .models import MonitoringSignal

    (MonitoringSignal.objects
     .filter(patient=task.patient,
             kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
             subject_key=f'task:{task.pk}',
             resolved_at__isnull=True)
     .update(resolved_at=timezone.now()))


def evaluate_patient(patient) -> list:
    """Every active task for one patient."""
    from .models import CareTask

    raised = []
    for task in CareTask.objects.filter(patient=patient, is_active=True):
        raised.extend(evaluate_task(task))
    return raised


def evaluate_all() -> list:
    from .models import CareTask

    raised = []
    for task in CareTask.objects.filter(is_active=True).select_related('patient'):
        raised.extend(evaluate_task(task))
    return raised


# ── Human-reported input ──────────────────────────────────────────────────────

def signal_for_report(report) -> list:
    """
    A person said something. That is itself the signal.

    No streak and no threshold: someone typing "I feel dizzy" into a care app
    has already decided it is worth mentioning, and making them say it three
    times before anyone hears would be a poor way to treat that.

    The signal records that a symptom was REPORTED. It does not record dizziness
    as a finding, and nothing downstream may present it as one — the caregiver
    is told their parent reported something, and the words stay behind the
    patient's sharing scope.
    """
    from .models import MonitoringSignal, PatientReport

    config = policy()
    if report.kind != PatientReport.Kind.SYMPTOM:
        return []
    if not config.notify_caregiver_on_reported_symptom:
        return []

    signal = MonitoringSignal.objects.create(
        patient      = report.patient,
        kind         = MonitoringSignal.Kind.REPORTED_SYMPTOM,
        severity     = MonitoringSignal.Severity.ATTENTION,
        window_start = report.effective_at,
        window_end   = report.effective_at,
        # Per-report rather than per-patient: two different symptoms on one day
        # are two things to hear about, and deduplicating them would drop one.
        subject_key  = f'report:{report.pk}',
        rule         = 'reported_symptom',
    )
    signal.reports.set([report])
    return [signal]


def signal_for_missed(occurrence) -> list:
    """
    The patient said they missed it. Different in kind from silence.

    No streak, because this is not an inference — they told us. It is also the
    case that most warrants hearing about early, since it is the one where the
    person has actively reached out.
    """
    from .models import MonitoringSignal, TaskOccurrence

    config = policy()
    if occurrence.state != TaskOccurrence.State.MISSED:
        return []
    if not config.notify_caregiver_on_reported_missed:
        return []

    signal = MonitoringSignal.objects.create(
        patient      = occurrence.patient,
        kind         = MonitoringSignal.Kind.REPORTED_MISSED,
        severity     = MonitoringSignal.Severity.ATTENTION,
        window_start = occurrence.due_at,
        window_end   = occurrence.due_at,
        subject_key  = f'occurrence:{occurrence.pk}',
        rule         = 'reported_missed',
    )
    signal.occurrences.set([occurrence])
    return [signal]

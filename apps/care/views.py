"""
The two faces of care monitoring: the patient answering, the caregiver watching.

The patient's side is built for one tap. Someone who has just taken a tablet
should not have to navigate anywhere, and the three answers are deliberately
"Done", "Skipped" and "Missed" rather than a single "Done" button — a system
offering only confirmation learns nothing from the person who did not do it, and
then has to guess.

The caregiver's side answers one question: does my parent need my attention? It
is not a record browser. A caregiver who wants the underlying documents goes
through the sharing pages, where the patient's scopes are enforced per section;
this page shows attention state, and nothing about it is a substitute for
clinical access.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required

from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import CareTask, MonitoringSignal, PatientReport, TaskOccurrence

logger = logging.getLogger(__name__)


# ── Patient ───────────────────────────────────────────────────────────────────

@login_required
def my_care(request):
    """
    The Care hub: what I have to do, and who I am looking after.

    One page for both directions of caring. Before this there were three —
    "My care" (my reminders), "Watching Over" (people who share with me) and
    "Sharing" (people I share with) — which is the application's internal
    structure showing through. A person experiences those as one subject.

    The data is assembled by `dashboard.hubs.care_overview` so the same shape
    can serve a future API, and so authorization stays in one place: the people
    list goes through `accounts.authz.shared_with` and nothing else.
    """
    from apps.dashboard.hubs import care_overview

    return render(request, 'care/hub.html', care_overview(request))


@login_required
def person(request, pk):
    """
    Care -> Anna. The canonical page about one person.

    Thin on purpose: `accounts.shared_patient` already implements every scope,
    the frozen-share cutoff and the access logging, and has the adversarial
    suite to prove it. Re-implementing any of that here to get a nicer URL is
    how two authorization paths come into existence.
    """
    from apps.accounts.views import shared_patient

    return shared_patient(request, pk)


@login_required
def respond(request, pk):
    """
    Record the patient's answer to one occurrence.

    Ownership is the filter, not a check after the fact: an occurrence belonging
    to someone else is not found rather than found-and-refused, so the endpoint
    cannot be used to discover that a given id exists.

    `resolve()` rejects anything outside HUMAN_STATES, so a crafted POST cannot
    talk the system into recording "unconfirmed" as though the person had said
    it — that state means the system heard nothing, and a request IS hearing
    something.
    """
    if request.method != 'POST':
        return redirect('dashboard:my_health')

    occurrence = get_object_or_404(TaskOccurrence, pk=pk, patient=request.user)
    state = (request.POST.get('state') or '').strip()

    try:
        occurrence.resolve(state, by=request.user,
                           input_method=PatientReport.InputMethod.WEB)
    except ValueError:
        messages.error(request, 'That is not a valid response.')
        return redirect('dashboard:my_health')

    # A reported miss is a stronger fact than silence and is acted on at once,
    # rather than waiting for the next scheduled cycle — the person has actively
    # told us something.
    if occurrence.state == TaskOccurrence.State.MISSED:
        _raise_and_dispatch(lambda: _missed_signal(occurrence))

    messages.success(request, 'Thanks — noted.')
    return redirect('dashboard:my_health')


@login_required
def add_task(request):
    """
    Create a reminder. Owned by the patient, always.

    The care feature shipped with no way to create a task at all — the page
    listed occurrences of schedules that nothing could produce, so the whole
    medication flow was unreachable. This is that missing step.

    Deliberately still patient-only. A caregiver setting up someone else's
    medication schedule is a care decision, and this codebase keeps that
    authority with the person whose body it is; the seam for caregiver-proposed
    tasks stays unbuilt rather than half-built.
    """
    if request.method != 'POST':
        return redirect('dashboard:my_health')

    label = (request.POST.get('label') or '').strip()
    if not label:
        messages.error(request, 'Please give the reminder a name.')
        return redirect('dashboard:my_health')

    times = [t.strip() for t in (request.POST.get('times') or '').split(',') if t.strip()]
    valid = [t for t in times if _looks_like_time(t)]
    if not valid:
        messages.error(request, 'Please give at least one time, like 08:00.')
        return redirect('dashboard:my_health')

    task = CareTask.objects.create(
        patient=request.user, label=label[:120],
        kind=CareTask.Kind.MEDICATION, times_of_day=valid)

    # Materialise straight away rather than waiting for the next scheduled run.
    # Someone who has just set up an 08:00 reminder and sees an empty page has
    # no way to tell whether it worked.
    from .scheduling import generate_occurrences

    generate_occurrences(patient=request.user)

    messages.success(request, f'“{task.label}” added.')
    return redirect('dashboard:my_health')


def _looks_like_time(value: str) -> bool:
    """HH:MM, 24-hour. Rejected rather than coerced — 25:00 is a typo, not 01:00."""
    parts = value.split(':')
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


@login_required
def stop_task(request, pk):
    """
    Stop a reminder without erasing what it already recorded.

    Deactivated, never deleted: the occurrences it produced are the record of
    what was asked and answered, and a caregiver looking at last week's history
    should not find it rewritten because the schedule changed today.
    """
    if request.method != 'POST':
        return redirect('dashboard:my_health')

    task = get_object_or_404(CareTask, pk=pk, patient=request.user)
    task.is_active = False
    task.save(update_fields=['is_active', 'updated_at'])

    # Future occurrences are removed; past ones stay as evidence.
    TaskOccurrence.objects.filter(
        task=task, due_at__gt=timezone.now(),
        state=TaskOccurrence.State.PENDING).delete()

    messages.success(request, f'“{task.label}” stopped.')
    return redirect('dashboard:my_health')


@login_required
def report(request):
    """
    Record what the patient says about how they are, in their own words.

    Stored verbatim. Nothing here classifies the text, scores it, or turns it
    into a symptom code: that would be an interpretation, and it would arrive
    with the authority of the patient's own voice while not being their words.
    """
    if request.method != 'POST':
        return redirect('dashboard:my_health')

    text = (request.POST.get('text') or '').strip()
    if not text:
        messages.error(request, 'Please say a few words first.')
        return redirect('dashboard:my_health')

    entry = PatientReport.objects.create(
        patient=request.user,
        reported_by=request.user,
        reported_by_role=PatientReport.Reporter.PATIENT,
        kind=PatientReport.Kind.SYMPTOM,
        input_method=PatientReport.InputMethod.WEB,
        text=text[:4000],
    )
    _raise_and_dispatch(lambda: _report_signal(entry))

    messages.success(request, 'Thanks for letting us know.')
    return redirect('dashboard:my_health')


# ── Caregiver ─────────────────────────────────────────────────────────────────

@login_required
def watching(request):
    """
    Folded into the Care hub.

    "Watching Over" was a third page about caring, beside "My care" and
    "Sharing". One subject, three entry points, three names. Kept as a redirect
    because links to it exist in notifications already delivered.
    """
    return redirect('care:my_care', permanent=True)


@login_required
def watch_detail(request, pk):
    """
    Kept as a permanent redirect to the one page about a person.

    There were two caregiver detail pages for the same human being: this one
    (care signals) and accounts.shared_patient (records, alerts, appointments,
    medications, with data_cutoff). Both asked "may I see this person", in two
    places, with two implementations of the scope rule. That is how two answers
    start disagreeing, and it left a caregiver guessing which page was the real
    one.

    shared_patient won because it already handles every scope and the frozen-
    share cutoff, and has the test suite to prove it. The care section moved
    there. This URL survives because links to it exist — in notifications
    already delivered, and in anything a caregiver bookmarked.
    """
    from django.shortcuts import redirect

    return redirect('care:person', pk=pk, permanent=True)


# ── Presentation helpers ──────────────────────────────────────────────────────
#
# _present() and _recent_activity() moved to accounts.views alongside
# shared_patient, which is now the single page about a person. Keeping copies
# here would mean two renderings of the same signal drifting apart.

def _open_signal_count(subject):
    return MonitoringSignal.objects.filter(
        patient=subject, resolved_at__isnull=True).count()


# ── Wiring ────────────────────────────────────────────────────────────────────

def _missed_signal(occurrence):
    from .signals_rules import signal_for_missed
    return signal_for_missed(occurrence)


def _report_signal(entry):
    from .signals_rules import signal_for_report
    return signal_for_report(entry)


def _raise_and_dispatch(make_signals):
    """
    Raise signals and notify, without letting either break the patient's action.

    The patient has already told us something true and it is already saved. If
    the notification pipeline fails, their answer must still stand — losing the
    record because a mail server was down would be the worst possible trade.
    """
    from apps.notifications.dispatch import dispatch_signal

    try:
        for signal in make_signals() or []:
            dispatch_signal(signal)
    except Exception:
        logger.exception('Care signal dispatch failed after a patient action')

"""
The dashboard's data contract: what is happening, and what needs doing.

Kept out of the view so the same answer serves the web page and, later, the
mobile API — and so the rules about what may be SAID live next to the rules
about what may be SHOWN.

Design constraints this file exists to hold:

  * Nothing is invented. Every number comes from a row. There is no health
    score, no adherence percentage, no composite index — none of those has a
    defensible basis here, and a number on a health dashboard is read as a
    clinical judgement whether or not one was meant.
  * Absence is never rendered as good news. A patient with no records gets
    NO_DATA, not a green tick.
  * Caregiver sections go through `accounts.authz`. There is no second
    authorization path in this module — one sharing rule, asked freshly.
  * A caregiver's summary carries no clinical content. It says whether someone
    needs attention; the person's page, behind their scopes, says why.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from .state import Item, Section, State

logger = logging.getLogger(__name__)


# ── Today's care ──────────────────────────────────────────────────────────────

def medication_section(patient) -> Section:
    """
    Doses due today and what became of them.

    "Not confirmed" is reported as exactly that. The system knows nobody
    answered; it does not know the dose was skipped, and a dashboard saying
    "missed" on that evidence would be asserting something about the patient
    that no row supports.
    """
    from apps.care.models import CareTask, TaskOccurrence

    section = Section(key='medications', title='Medications',
                      href='/care/', state=State.NO_DATA)

    if not CareTask.objects.filter(patient=patient, is_active=True).exists():
        section.summary = 'No medication reminders set up.'
        return section

    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    today = (TaskOccurrence.objects
             .filter(patient=patient, due_at__gte=start,
                     due_at__lt=start + timedelta(days=1))
             .select_related('task').order_by('due_at'))

    rows = list(today)
    if not rows:
        section.summary = 'Nothing scheduled for today.'
        return section

    confirmed   = [o for o in rows if o.state == TaskOccurrence.State.CONFIRMED]
    unconfirmed = [o for o in rows if o.state == TaskOccurrence.State.UNCONFIRMED]
    pending     = [o for o in rows if o.state == TaskOccurrence.State.PENDING]

    section.summary = f'{len(confirmed)} of {len(rows)} confirmed today'

    if unconfirmed:
        section.items.append(Item(
            title=f'{len(unconfirmed)} not confirmed',
            state=State.ATTENTION,
            # The wording is the whole point. See the module docstring.
            detail='The app did not hear back. Only you can say what happened.',
            href='/care/', action=('Update now', '/care/')))

    upcoming = next((o for o in pending if o.due_at >= timezone.now()), None)
    if upcoming is not None:
        section.items.append(Item(
            title=f'Next: {upcoming.task.label}',
            state=State.UPCOMING,
            detail=timezone.localtime(upcoming.due_at).strftime('%H:%M'),
            href='/care/'))

    if not section.items:
        section.items.append(Item(title='All confirmed today', state=State.OK))

    return section.settle()


def appointment_section(patient) -> Section:
    """The next appointment, or an honest empty state."""
    from apps.appointments.models import Appointment

    section = Section(key='appointments', title='Appointments',
                      href='/appointments/', state=State.NO_DATA)

    nxt = (Appointment.objects
           .filter(patient=patient, is_cancelled=False,
                   appointment_datetime__gte=timezone.now())
           .order_by('appointment_datetime').first())

    if nxt is None:
        section.summary = 'No upcoming appointments.'
        return section

    when = timezone.localtime(nxt.appointment_datetime)
    where = ' · '.join(p for p in (nxt.doctor_name, nxt.location) if p)
    # Built by hand rather than with strftime's %-d / %-H: those are a glibc
    # extension, and on Windows strftime raises ValueError for them. A dashboard
    # that crashes for every developer on the team the moment an appointment
    # exists is not a formatting preference.
    section.summary = f'{when:%a} {when.day} {when:%b}, {when:%H:%M}'
    section.items.append(Item(
        title=nxt.title, state=State.UPCOMING,
        detail=' · '.join(p for p in (f'{when:%A} {when:%H:%M}', where) if p),
        href='/appointments/'))
    return section.settle()


def recent_measurements(patient, limit: int = 6) -> list:
    """
    What has actually been measured lately, most recent first.

    Grouped by KIND rather than listed row by row: a wearable import writes
    hundreds of heart-rate points and a reader wants "heart rate — today", not
    two hundred lines. The date is the newest of each kind.

    Blood pressure appears here only if such data exists. There is no
    blood-pressure metric in this system today — `WearableDataPoint.Metric` runs
    heart rate, steps, sleep, calories, blood oxygen, weight, temperature — and
    no ingestion path produces one, so a hardcoded "Blood pressure — Today" row
    would be a fabricated reading on a health dashboard. If BP arrives as a lab
    value it shows up through the lab branch below, named as the document named
    it.
    """
    from apps.medical_records.models import ParsedLabValue, WearableDataPoint

    rows = []

    seen = set()
    points = (WearableDataPoint.objects
              .filter(patient=patient)
              .order_by('-recorded_at')
              .select_related()[:400])
    for point in points:
        if point.metric in seen:
            continue
        seen.add(point.metric)
        rows.append({'label': point.get_metric_display(),
                     'when': point.recorded_at,
                     'href': '/records/'})

    latest_lab = (ParsedLabValue.objects
                  .filter(patient=patient)
                  .order_by('-measured_at', '-id').first())
    if latest_lab is not None:
        when = latest_lab.measured_at or latest_lab.record.uploaded_at
        rows.append({'label': 'Lab results', 'when': when, 'href': '/records/'})

    rows = [r for r in rows if r['when'] is not None]
    rows.sort(key=lambda r: r['when'], reverse=True)
    for row in rows:
        row['ago'] = _day_ago(row['when'])
    return rows[:limit]


def care_section(patient) -> Section:
    """
    How the patient is doing, as far as anything has been recorded.

    Document conflicts used to appear here too; they went with the medications
    & conditions feature. Removing that also removed the only try/except in
    this function, which turned a failure in one card into a 500 for the whole
    dashboard — so the guard is back, around the queries that remain.
    """
    from apps.care.models import MonitoringSignal, PatientReport

    section = Section(key='care', title='My care', href='/care/',
                      state=State.NO_DATA)

    try:
        open_signals = list(MonitoringSignal.objects
                            .filter(patient=patient, resolved_at__isnull=True)
                            .order_by('-created_at')[:5])

        # The most recent measurement leads, because "when was I last measured"
        # is the question this card is opened to answer. It is whatever exists
        # — this system has no blood-pressure metric, so naming one here would
        # invent a reading nobody took.
        measurements = recent_measurements(patient, limit=1)
        # Fetched unconditionally: it is read below whichever branch runs, and
        # assigning it only inside the `else` made every dashboard with a
        # measurement raise UnboundLocalError.
        last_report = (PatientReport.objects.filter(patient=patient)
                       .order_by('-created_at').first())
    except Exception:
        # One card failing must not take the page down, and must not read as
        # "nothing is wrong" either. UNAVAILABLE says the app does not know,
        # which is the honest answer and is never rendered as reassurance.
        logger.exception('care_section failed for patient %s', patient.pk)
        section.items.append(Item(
            title='Care summary unavailable', state=State.UNAVAILABLE,
            detail='This could not be loaded just now.'))
        return section.settle()

    for signal in open_signals:
        section.items.append(Item(
            title=signal.get_kind_display(), state=State.ATTENTION,
            detail=_signal_detail(signal), href='/care/'))

    if measurements:
        newest = measurements[0]
        section.summary = f'{newest["label"]} · last measured {newest["ago"].lower()}'
    elif last_report is not None:
        section.summary = f'Last update {_ago(last_report.created_at)}'

    if not section.items:
        # "Nothing needs attention" is only honest when something was actually
        # looked at. With no measurement and no report there is nothing to
        # reassure anyone about, and saying so anyway is the false-reassurance
        # failure this dashboard is built to avoid.
        if last_report is not None or measurements:
            section.items.append(Item(title='Nothing needs attention', state=State.OK))
        else:
            section.summary = section.summary or 'Nothing recorded yet.'
    return section.settle()


def alerts_section(patient) -> Section:
    """
    Clinically defined alerts — the only source of URGENT on this dashboard.

    Everything else this product produces is an observation about its own
    records. A critical HealthAlert is the one signal a rule author intended as
    clinically meaningful, so it is the one thing allowed to say "now".
    """
    from apps.ai_insights.models import HealthAlert

    section = Section(key='alerts', title='Health alerts',
                      href='/insights/', state=State.NO_DATA)

    unread = list(HealthAlert.objects.filter(patient=patient, is_read=False)
                  .order_by('-created_at')[:5])
    if not unread:
        section.summary = 'No unread alerts.'
        if HealthAlert.objects.filter(patient=patient).exists():
            section.items.append(Item(title='No unread alerts', state=State.OK))
        return section.settle()

    for alert in unread:
        section.items.append(Item(
            title=alert.title,
            state=(State.URGENT if alert.severity == HealthAlert.Severity.CRITICAL
                   else State.ATTENTION),
            detail=alert.get_severity_display(), href='/insights/'))
    section.summary = f'{len(unread)} unread'
    return section.settle()


# ── Caregiver ─────────────────────────────────────────────────────────────────

def watching_section(caregiver) -> Section:
    """
    People who have asked this person to keep an eye on them.

    Deliberately carries no clinical content — a name and whether they need
    attention. What is wrong, if anything, lives on their page behind the scopes
    they chose. A dashboard summary that leaked "unconfirmed metformin" would
    hand out a diagnosis to anyone glancing at the screen.

    Authorization comes from `accounts.authz.shared_with`; there is no second
    rule here to drift out of step with the first.
    """
    from apps.accounts.authz import shared_with
    from apps.care.models import MonitoringSignal
    from apps.notifications.recipients import CARE_SCOPE

    section = Section(key='watching', title='People I look after',
                      href='/care/', state=State.NO_DATA)

    try:
        subjects = list(shared_with(caregiver, CARE_SCOPE))
    except Exception:
        logger.exception('shared_with failed for caregiver %s', caregiver.pk)
        section.items.append(Item(title='Could not be loaded',
                                  state=State.UNAVAILABLE))
        return section.settle()

    if not subjects:
        return section          # NO_DATA; the section is hidden by the view

    for subject in subjects:
        open_count = MonitoringSignal.objects.filter(
            patient=subject, resolved_at__isnull=True).count()
        latest = (MonitoringSignal.objects.filter(patient=subject)
                  .order_by('-created_at').first())

        if open_count:
            state  = State.ATTENTION
            detail = ('1 thing needs attention' if open_count == 1
                      else f'{open_count} things need attention')
        elif latest is not None:
            state  = State.OK
            detail = f'No urgent issues · last activity {_ago(latest.created_at)}'
        else:
            # Sharing exists but nothing has ever been recorded. Not "fine".
            state, detail = State.NO_DATA, 'No care activity recorded yet'

        section.items.append(Item(
            title=_display_name(subject), state=state, detail=detail,
            href=f'/care/person/{subject.pk}/'))

    section.items.sort(key=lambda i: i.rank)
    needing = sum(1 for i in section.items if i.is_actionable)
    section.summary = (f'{needing} of {len(section.items)} need attention'
                       if needing else 'Nobody needs attention right now')
    return section.settle()


# ── Assembly ──────────────────────────────────────────────────────────────────

def _section_or_unavailable(build, user, *, key, title, href=None) -> Section:
    """
    Run a section builder, turning a crash into an honest "we don't know".

    The distinction this preserves is the one the whole state model exists for:
    UNAVAILABLE means the app could not find out, which is not OK and is not
    NO_DATA either. A section that failed must never be counted as evidence that
    nothing is wrong, and must never be silently omitted — a missing card reads
    as "nothing here", which is the false reassurance this page is built to
    avoid.
    """
    try:
        return build(user)
    except Exception:
        logger.exception('%s section failed for %s', key, getattr(user, 'pk', None))
        section = Section(key=key, title=title, href=href,
                          state=State.UNAVAILABLE)
        section.items.append(Item(
            title=f'{title} unavailable', state=State.UNAVAILABLE,
            detail='This could not be loaded just now.'))
        return section.settle()


def build_dashboard(user) -> dict:
    """
    Everything the dashboard renders, already decided.

    The template's job is to lay this out, not to work out what it means. A
    template deciding whether something is urgent is a rule nobody can test.
    """
    # Every builder is called through the same guard, because the dashboard is
    # the page a patient opens to find out whether anything is wrong. One
    # subsystem raising used to return a 500 for the whole page — the reader
    # then learns nothing at all, which is strictly worse than learning that one
    # card is unavailable. UNAVAILABLE is excluded from `checked` below, so a
    # failed section can never contribute to "nothing needs your attention".
    sections = [
        _section_or_unavailable(alerts_section, user, key='alerts',
                                title='Health alerts', href='/insights/'),
        _section_or_unavailable(medication_section, user, key='medications',
                                title='Medications', href='/dashboard/health/'),
        _section_or_unavailable(appointment_section, user, key='appointments',
                                title='Appointments', href='/appointments/'),
        _section_or_unavailable(care_section, user, key='care',
                                title='My care', href='/care/'),
    ]
    watching = _section_or_unavailable(watching_section, user, key='watching',
                                       title='People I look after', href='/care/')

    # "Needs your attention" is assembled from the sections rather than queried
    # separately, so it can never disagree with the section it came from.
    #
    # Watching-over items are deliberately NOT included. They have their own
    # section, which lists everyone with their state and is promoted above the
    # fold when someone needs help — so folding them in here put the same person
    # on screen twice, once in the digest and once in their own row. One fact,
    # one place; the ordering carries the urgency instead.
    attention = []
    for section in sections:
        for item in section.items:
            if item.is_actionable:
                attention.append((section, item))
    attention.sort(key=lambda pair: pair[1].rank)

    # What the patient has not set up yet, and where to do it.
    #
    # This exists because the page was otherwise willing to say "nothing needs
    # your attention" to someone with no medication reminders and no records —
    # reassurance derived from having nothing to check, which is the failure the
    # NO_DATA state was introduced to prevent and which slipped back in one
    # level up, at the banner.
    setup_gaps = [
        {'label': label, 'href': href}
        for key, label, href in (
            ('medications', 'Set up medication reminders', '/care/'),
            ('appointments', 'Add an appointment', '/appointments/'),
        )
        if sections_by_key(sections, key).state == State.NO_DATA
    ]

    checked = [s for s in sections if s.state not in (State.NO_DATA, State.UNAVAILABLE)]

    # Computed once. Both the reassurance banner and the caller need it, and two
    # copies of the same expression is how they end up disagreeing.
    watching_needs_help = bool(watching.items) and watching.state in State.ACTIONABLE

    try:
        measurements = recent_measurements(user)
    except Exception:
        # The strip at the foot of the page. Empty is honest here: the sections
        # above already carry the state, and this list asserts nothing on its
        # own.
        logger.exception('recent_measurements failed for %s', user.pk)
        measurements = []

    return {
        'greeting':      greeting(),
        'measurements':  measurements,
        'sections':      {s.key: s for s in sections},
        'watching':      watching,
        'is_caregiver':  bool(watching.items),
        'watching_urgent': watching_needs_help,
        'attention':     attention,
        'overall':       State.worst([s.state for s in sections]),
        'has_any_data':  bool(checked),
        'setup_gaps':    setup_gaps,
        # True only when everything that could raise a concern actually reported
        # one way or the other. Anything less and the calm line is qualified.
        'fully_checked': bool(checked) and not setup_gaps,
        # Whether to say anything reassuring at all.
        #
        # The banner is computed from this person's OWN health, so a caregiver
        # whose parent needed help was told "nothing needs your attention"
        # directly above a row saying someone did. Reassurance has to account
        # for everything on the page, not just the half it was derived from.
        'show_reassurance': not attention and not watching_needs_help,
    }


def sections_by_key(sections, key):
    for section in sections:
        if section.key == key:
            return section
    return Section(key=key, title=key, state=State.UNAVAILABLE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _signal_detail(signal) -> str:
    """Describe a signal in terms of what was observed, never a conclusion."""
    from apps.care.models import MonitoringSignal

    if signal.kind == MonitoringSignal.Kind.REPEATED_UNCONFIRMED:
        count = signal.occurrences.count()
        return (f'{count} reminder{"" if count == 1 else "s"} in a row went '
                f'unanswered. That means no one replied — not that anything '
                f'was missed.')
    if signal.kind == MonitoringSignal.Kind.REPORTED_MISSED:
        return 'You told us you missed a scheduled task.'
    if signal.kind == MonitoringSignal.Kind.REPORTED_SYMPTOM:
        return 'You reported how you were feeling.'
    return ''


def _display_name(user) -> str:
    first = (getattr(user, 'first_name', '') or '').strip()
    if first:
        return first
    full = (user.get_full_name() or '').strip() if hasattr(user, 'get_full_name') else ''
    return full.split()[0] if full else 'Family member'


def greeting() -> str:
    """
    Time of day in the reader's timezone, not the server's.

    A patient in Helsinki opening the app at 9am should not be wished good
    evening because the process runs in UTC.
    """
    hour = timezone.localtime().hour
    if hour < 12:
        return 'Good morning'
    if hour < 18:
        return 'Good afternoon'
    return 'Good evening'


def _day_ago(when) -> str:
    """
    Calendar-relative, not elapsed hours.

    "Yesterday" has to mean the previous calendar day: something measured at
    23:00 last night is yesterday at 9am this morning, even though only ten
    hours have passed.
    """
    today = timezone.localdate()
    day = timezone.localtime(when).date()
    delta = (today - day).days
    if delta <= 0:
        return 'Today'
    if delta == 1:
        return 'Yesterday'
    if delta < 7:
        return f'{delta} days ago'
    if delta < 14:
        return 'Last week'
    # Day and month built by hand: strftime('%-d') is a glibc extension that
    # raises ValueError on Windows.
    local = timezone.localtime(when)
    return f'{local.day} {local:%b}'


def _ago(when) -> str:
    """Coarse and human. Precision here would imply monitoring we do not do."""
    delta = timezone.now() - when
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return 'just now'
    if minutes < 60:
        return f'{minutes} minute{"" if minutes == 1 else "s"} ago'
    hours = minutes // 60
    if hours < 24:
        return f'{hours} hour{"" if hours == 1 else "s"} ago'
    days = hours // 24
    return f'{days} day{"" if days == 1 else "s"} ago'

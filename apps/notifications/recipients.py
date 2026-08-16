"""
Who may be told, and how much.

Two questions, answered separately and in this order:

  1. is this person entitled to hear about the subject at all?
  2. given they are, what is the least that answers "does my parent need me"?

The first is authorization and reuses `accounts.authz` rather than restating it.
A second copy of the sharing rule living in the notification layer is how the
two drift apart, and the one that drifts is the one that keeps sending after a
patient has revoked.

The second is minimisation, and it is the reason this file exists at all. A
notification is the least controlled surface in the product: it lands on a lock
screen, in an inbox, on a watch face, and it is read by whoever is holding the
device. "Your mother has not confirmed her 08:00 metformin" tells a bystander
that she is diabetic. "Your mother has not confirmed a scheduled medication for
3 days" tells the person who needs to know what they need to do, and tells a
bystander nothing.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Sharing scope a caregiver needs to be told about care monitoring.
#:
#: `alerts` rather than `records`, deliberately. The patient's own description
#: of that scope is "tell me if something is wrong without letting them read my
#: file", which is exactly what a care notification is. Requiring `records`
#: would mean a caregiver could only be alerted if they were also given the
#: documents, which forces an all-or-nothing disclosure the patient did not ask
#: for.
CARE_SCOPE = 'alerts'


def caregivers_for(subject) -> list:
    """
    Everyone the patient has authorised to hear that something needs attention.

    Goes through `sharing_grant`, so revocation, expiry and scope are all
    honoured without this module knowing how any of them work. A patient who
    revokes a share stops generating deliveries on the next event, with no
    separate notification-preferences record to fall out of step.
    """
    from apps.accounts.authz import sharing_grant
    from apps.accounts.models import SharingGrant

    recipients = []
    candidates = (SharingGrant.objects
                  .filter(patient=subject)
                  .select_related('recipient'))
    for grant in candidates:
        # Re-asked through the predicate rather than trusting the row: it is the
        # predicate that knows about expiry and unusable dates, and it fails
        # closed where a raw filter would not.
        if sharing_grant(grant.recipient, subject, CARE_SCOPE) is not None:
            recipients.append(grant.recipient)
    return recipients


# ── Minimisation ──────────────────────────────────────────────────────────────

def _plural(count, singular, plural=None):
    return singular if count == 1 else (plural or singular + 's')


def render_for_caregiver(event) -> tuple[str, str, str]:
    """
    What a caregiver is told: what needs their attention, and nothing else.

    Never includes the medication name, the symptom words, a diagnosis, a lab
    value, a document title, or a time of day. Those are all things a caregiver
    may well be entitled to see — but entitlement is not the test here. The test
    is whether the notification NEEDS them to do its job, and it does not: its
    job is to get someone to look, and the app is where they look.

    Symptom wording is the sharpest case. "I felt dizzy" is the patient's own
    account and the thing they most plausibly wanted heard — and it is also a
    sentence about their health arriving unprompted on someone else's phone. It
    stays behind the sharing scope, in the app, where the patient's choices
    about who sees what are actually enforced.
    """
    from apps.care.models import MonitoringSignal

    name = _display_name(event.subject)
    kind = event.signal.kind if event.signal_id else None
    # The page about THIS person. It used to be a hardcoded '/care/', which is
    # the recipient's own care page - so a notification about someone's mother
    # opened the reader's own medication list.
    where = f'/care/person/{event.subject_id}/'

    if kind == MonitoringSignal.Kind.REPEATED_UNCONFIRMED:
        count = event.signal.occurrences.count()
        return (
            'Check in on ' + name,
            f'{name} has not confirmed a scheduled care task '
            f'{count} {_plural(count, "time")}. Open HealthCompass to see what '
            f'needs attention.',
            where,
        )

    if kind == MonitoringSignal.Kind.REPORTED_MISSED:
        return (
            'Check in on ' + name,
            f'{name} reported missing a scheduled care task. '
            f'Open HealthCompass for details.',
            where,
        )

    if kind == MonitoringSignal.Kind.REPORTED_SYMPTOM:
        # Deliberately does not repeat what they said.
        return (
            'Check in on ' + name,
            f'{name} reported how they are feeling. '
            f'Open HealthCompass to read it.',
            where,
        )

    return ('Check in on ' + name,
            f'Something about {name} needs your attention in HealthCompass.',
            where)


def render_for_patient(event) -> tuple[str, str, str]:
    """
    What the patient is told about their own situation.

    They may see everything about themselves, so the constraint here is not
    disclosure but tone. A reminder that says "you missed your medication" makes
    a claim the system cannot support and reads as an accusation; the system
    knows only that it has not heard back.
    """
    from apps.care.models import MonitoringSignal

    kind = event.signal.kind if event.signal_id else None

    if kind == MonitoringSignal.Kind.REPEATED_UNCONFIRMED:
        return ('A reminder is waiting',
                'There are care tasks you have not marked as done. '
                'Open HealthCompass to update them.',
                '/care/')

    return ('HealthCompass', 'Something needs your attention.', '/care/')


def _display_name(user) -> str:
    """
    A name for the notification. Falls back rather than exposing a username.

    Usernames in this system are often the email local-part, which is more
    identifying than a first name and is not what a family member calls anyone.
    """
    first = (getattr(user, 'first_name', '') or '').strip()
    if first:
        return first
    full = (user.get_full_name() or '').strip() if hasattr(user, 'get_full_name') else ''
    if full:
        return full.split()[0]
    return 'Your family member'

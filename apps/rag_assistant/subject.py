"""
Whose records a question is answered from.

Until now the answer was structural: `ask()` and `stream_ask()` were only ever
called with `request.user`, so no input could point them at another person. That
was a real guarantee and it is now gone on purpose — a caregiver wanting to ask
"has my mother taken her tablets?" is the whole point of family sharing.

What replaces it is this module plus `accounts.authz.can_ask_assistant_about`.
The rules that matter:

  * The subject arrives as an id from the client and is therefore untrusted.
    Nothing here trusts it; it is resolved and then checked, and a failure is
    indistinguishable from the person not existing.
  * The check is stricter than viewing their care page, because answering
    transmits their record excerpts to an external LLM. It needs the RECORDS
    scope and the SUBJECT's own consent — not the asker's.
  * Every cross-person question is written to the subject's access trail. "Who
    has looked at my data" has to include "my daughter asked an AI about me".

`ALL` is deliberately not a merged context. Asking one question across three
people and pouring their records into a single prompt produces answers nobody
can attribute — "the current medications" with no way to tell whose. Each
subject is retrieved and answered separately, and the caller labels them.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Sentinel meaning "everyone I may ask about", answered per person.
ALL = 'all'

#: Sentinel meaning the asker themselves. Also the default.
SELF = 'self'


class SubjectNotPermitted(Exception):
    """The asker may not ask about this person, or the person does not exist."""


def resolve(asker, raw):
    """
    Turn the client's `subject` value into the people to answer about.

    Returns a list, because ALL answers per person rather than merging. A
    single subject returns a one-item list so callers have one shape to handle.
    """
    from apps.accounts.authz import assistant_subjects, can_ask_assistant_about

    value = (str(raw or '').strip() or SELF)

    if value == SELF:
        return [asker]

    if value == ALL:
        # Whatever they may currently ask about — recomputed per request, so a
        # share revoked a minute ago drops out without anything else noticing.
        return assistant_subjects(asker)

    from django.contrib.auth import get_user_model

    try:
        subject = get_user_model().objects.get(pk=value)
    except (get_user_model().DoesNotExist, ValueError, TypeError):
        # Same failure as "not permitted", on purpose: distinguishing them
        # would turn this into a way to find out which user ids exist.
        raise SubjectNotPermitted('unknown subject')

    if not can_ask_assistant_about(asker, subject):
        raise SubjectNotPermitted('not permitted')

    return [subject]


def record_access(asker, subject, query: str) -> None:
    """
    Note in the SUBJECT's trail that someone asked about them.

    Only for questions about somebody else — a patient asking about their own
    records is not an access event anyone needs to review, and logging it would
    bury the ones that matter.

    The question text is NOT stored here. It is the asker's words, it can carry
    clinical detail, and the access log is read by the subject to see who looked
    — not to read what was typed about them.
    """
    if asker.pk == subject.pk:
        return
    try:
        from apps.accounts.models import DoctorAccessLog

        DoctorAccessLog.objects.create(
            actor=asker, patient=subject, resource='assistant:question')
    except Exception:
        # Never let bookkeeping break the answer; the failure is logged so it
        # is not silent.
        logger.exception('Could not record assistant access to patient %s',
                         getattr(subject, 'pk', None))


def label_for(asker, subject) -> str:
    """How a subject is named in the UI and in an answer heading."""
    if asker.pk == subject.pk:
        return 'You'
    first = (getattr(subject, 'first_name', '') or '').strip()
    if first:
        return first
    full = (subject.get_full_name() or '').strip()
    return full.split()[0] if full else 'Family member'


def choices_for(asker) -> list:
    """
    The selector's options: yourself, each permitted person, and All.

    Built from the same predicate the request path enforces, so the list a user
    is offered cannot include something the check would then refuse.
    """
    from apps.accounts.authz import assistant_subjects

    people = assistant_subjects(asker)
    options = [{'value': SELF, 'label': 'My own data'}]
    options += [{'value': str(p.pk), 'label': label_for(asker, p)}
                for p in people if p.pk != asker.pk]
    if len(options) > 1:
        options.append({'value': ALL, 'label': 'Everyone'})
    return options

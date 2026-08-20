"""
The pipeline, in one place and in one order.

    signal -> event -> [authorization] -> recipients -> render -> channels

The order is the design. Authorization happens before rendering, so text that a
recipient is not entitled to cannot be built and then discarded — a string that
exists is a string that can be logged, cached, or attached to an exception.

Rendering is deliberately NOT per recipient. This docstring used to claim it
was, and `render_for_caregiver` has never taken a recipient: the call sat inside
the loop and recomputed the same string N times, so the promise was decorative.

The honest property is stronger than the one that was claimed. Caregiver text is
minimised until it carries nothing a sharing scope could vary — no medication
name, no symptom words, no value, no time of day, only a first name and "open
the app". There is nothing left for a per-recipient rule to decide, which is
why one render serves every authorised caregiver. Authorization decides WHETHER
someone is told; minimisation has already decided WHAT, identically for all of
them. `test_care_dispatch` pins this: two caregivers holding different scopes
must receive byte-identical text.

Nothing here decides what is worth saying; that was decided by the rules in
`care.signals_rules`. Nothing here decides how bytes reach a phone; that is a
channel. This module only sequences, and refuses to skip a step.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def _fold_into(event):
    """
    Record one more recurrence of a situation that is still the current one.

    `F()` rather than read-add-write. Two care cycles running at once both read
    the same count and one increment is lost — the same read-modify-write race
    that `run_count` had. Refreshed afterwards because the caller is handed the
    object and will read the value.
    """
    from django.db.models import F

    from .events import NotificationEvent

    NotificationEvent.objects.filter(pk=event.pk).update(
        occurrence_count=F('occurrence_count') + 1, updated_at=timezone.now())
    event.refresh_from_db(fields=['occurrence_count', 'updated_at'])
    return event


def event_for_signal(signal):
    """
    Create the notification event for a signal, or fold it into a live one.

    Aggregation is what stops a persistent situation becoming a stream of
    identical messages. A task unanswered for a week is one thing that is still
    true, not seven things that happened — so a repeat inside the window bumps
    `occurrence_count` on the existing event and does not produce a second round
    of deliveries.

    Concurrency
    -----------
    The window check is filter-then-create, which two concurrent cycles both
    pass. `NotificationEvent` therefore carries a partial unique constraint on
    (subject, dedupe_key) over live rows, and the loser of the race lands here
    in the IntegrityError branch and folds instead of delivering a second time.

    The constraint is what makes this correct; the check below is what makes it
    cheap. Neither is redundant — without the check every repeat would raise and
    be caught, and without the constraint the check is only usually true.
    """
    from datetime import timedelta

    from django.db import DatabaseError

    from apps.care.policy import policy

    from .events import NotificationEvent

    dedupe_key = f'{signal.kind}:{signal.subject_key}'
    window = timezone.now() - timedelta(hours=policy().resignal_cooldown_hours)

    live = (NotificationEvent.objects
            .filter(subject=signal.patient, dedupe_key=dedupe_key,
                    superseded_at__isnull=True)
            .order_by('-created_at')
            .first())

    if live is not None:
        if live.created_at >= window:
            return _fold_into(live), False
        # Older than the cooldown: the situation gets to be raised again, but
        # the previous event has to stop being live first or the constraint
        # would reject the new one. Superseding is not deleting — the old event
        # keeps its deliveries and its count.
        NotificationEvent.objects.filter(
            pk=live.pk, superseded_at__isnull=True).update(
                superseded_at=timezone.now())

    severity = {
        signal.Severity.INFO:      NotificationEvent.Severity.INFO,
        signal.Severity.ATTENTION: NotificationEvent.Severity.ATTENTION,
        signal.Severity.URGENT:    NotificationEvent.Severity.URGENT,
    }.get(signal.severity, NotificationEvent.Severity.ATTENTION)

    try:
        with transaction.atomic():
            event = NotificationEvent.objects.create(
                kind=NotificationEvent.Kind.CARE_SIGNAL,
                severity=severity,
                subject=signal.patient,
                signal=signal,
                dedupe_key=dedupe_key,
            )
        return event, True
    except (IntegrityError, DatabaseError):
        # Someone else created the live event between our check and our insert.
        # Fold into theirs. Reported as folded, so the caller does not deliver.
        winner = (NotificationEvent.objects
                  .filter(subject=signal.patient, dedupe_key=dedupe_key,
                          superseded_at__isnull=True)
                  .order_by('-created_at')
                  .first())
        if winner is None:
            # The row was superseded again in the meantime. Nothing to fold
            # into and nothing safely creatable; say so rather than guess.
            logger.warning('Lost the create race for %s and found no live event',
                           dedupe_key)
            raise
        logger.info('Concurrent event for %s folded into %s', dedupe_key, winner.pk)
        return _fold_into(winner), False


def deliver(event, *, channels=None) -> list:
    """
    Send one event to everyone entitled to it. Returns the delivery rows.

    The authorization call is not optional and not cached. It is asked fresh for
    every dispatch, so a share revoked five minutes ago stops the next message
    without anything else having to notice.
    """
    from .channels import default_channels, get_channel
    from .recipients import caregivers_for, render_for_caregiver

    channel_names = list(channels) if channels else default_channels()
    recipients = caregivers_for(event.subject)
    deliveries = []

    # Rendered once, outside the loop. The text carries nothing a sharing scope
    # could vary (see the module docstring), so rendering per recipient produced
    # N identical strings and implied a per-recipient rule that does not exist.
    # Built only after `caregivers_for` has run, so the ordering the module
    # promises — authorize, then render — still holds.
    if recipients:
        title, body, link = render_for_caregiver(event)

    for recipient in recipients:
        for name in channel_names:
            deliveries.append(
                _attempt(event, recipient, get_channel(name), name,
                         title, body, link))

    if not deliveries:
        logger.info('Event %s reached nobody — no authorised recipient', event.pk)
    return [d for d in deliveries if d is not None]


def deliver_to_patient(event, *, channels=None) -> list:
    """
    Tell the patient about their own situation.

    No authorization question: it is their data. Kept as a separate entry point
    so that the caregiver path cannot accidentally be reused for the patient and
    inherit a scope check that would silently drop their own notifications.
    """
    from .channels import default_channels, get_channel
    from .recipients import render_for_patient

    channel_names = list(channels) if channels else default_channels()
    title, body, link = render_for_patient(event)
    return [d for d in (
        _attempt(event, event.subject, get_channel(name), name, title, body, link)
        for name in channel_names) if d is not None]


def _attempt(event, recipient, channel, name, title, body, link=''):
    """
    One (event, recipient, channel) attempt, recorded whatever happens.

    An unavailable channel produces a row rather than nothing, so "we would have
    texted them if SMS existed" is visible in the database instead of being
    inferable only from an absence of rows.
    """
    from .events import NotificationDelivery

    if channel is None:
        logger.warning('Unknown notification channel %r', name)
        return None

    try:
        with transaction.atomic():
            delivery, created = NotificationDelivery.objects.get_or_create(
                event=event, recipient=recipient, channel=name,
                defaults={'title': title, 'body': body})
    except IntegrityError:
        return None
    if not created:
        # Already attempted. Re-sending on a retry would deliver the same worry
        # twice, which is the aggregation problem again one level down.
        return delivery

    if not channel.is_available():
        return delivery.mark(NotificationDelivery.Status.UNAVAILABLE,
                             detail=f'{name} is not configured in this environment')

    try:
        sent, detail = channel.deliver(
            recipient=recipient, title=title, body=body, event=event, link=link)
    except Exception as exc:
        # A channel must never take the pipeline down with it: the in-app row
        # for the same event has to survive a broken mail server.
        logger.exception('Channel %s raised while delivering event %s', name, event.pk)
        return delivery.mark(NotificationDelivery.Status.FAILED,
                             detail=type(exc).__name__)

    return delivery.mark(
        NotificationDelivery.Status.SENT if sent else NotificationDelivery.Status.FAILED,
        detail=detail)


class DispatchResult(list):
    """
    The deliveries, plus why there were none.

    An empty result had two very different causes and no way to tell them apart:
    the event was folded into a live one (working as intended, deliberate
    silence), or nobody was authorised to hear it (possibly a revoked share,
    possibly a patient with no caregivers, worth knowing about). Both returned
    `[]`.

    A list subclass rather than a new type, because `== []` and `len(...)` are
    what every existing caller and test already does, and changing that would be
    churn for a diagnostic. `.folded` and `.event` are additions, not a new
    contract.
    """

    def __init__(self, deliveries=(), *, folded=False, event=None):
        super().__init__(deliveries)
        self.folded = folded
        self.event = event

    @property
    def reached_nobody(self) -> bool:
        """Empty because no one was entitled to hear it, not because it folded."""
        return not self and not self.folded


def dispatch_signal(signal, *, channels=None) -> DispatchResult:
    """
    Signal in, deliveries out. The whole chain, for callers that want it.

    Returns a `DispatchResult`, which behaves as the list of deliveries it
    always was and additionally says whether an empty result means "folded into
    a live event" or "nobody was authorised".
    """
    event, created = event_for_signal(signal)
    if not created:
        # Folded into a live event. The count moved; nobody is messaged again.
        return DispatchResult(folded=True, event=event)
    return DispatchResult(deliver(event, channels=channels), event=event)

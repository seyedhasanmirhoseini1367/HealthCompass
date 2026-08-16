"""
The pipeline, in one place and in one order.

    signal -> event -> [authorization] -> recipients -> render -> channels

The order is the design. Authorization happens before rendering, so text that a
recipient is not entitled to cannot be built and then discarded — a string that
exists is a string that can be logged, cached, or attached to an exception.
Rendering happens per recipient, because what a caregiver may see depends on
what this patient shared with THAT caregiver.

Nothing here decides what is worth saying; that was decided by the rules in
`care.signals_rules`. Nothing here decides how bytes reach a phone; that is a
channel. This module only sequences, and refuses to skip a step.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def event_for_signal(signal):
    """
    Create the notification event for a signal, or fold it into a live one.

    Aggregation is what stops a persistent situation becoming a stream of
    identical messages. A task unanswered for a week is one thing that is still
    true, not seven things that happened — so a repeat inside the window bumps
    `occurrence_count` on the existing event and does not produce a second round
    of deliveries.
    """
    from apps.care.policy import policy
    from datetime import timedelta

    from .events import NotificationEvent

    dedupe_key = f'{signal.kind}:{signal.subject_key}'
    window = timezone.now() - timedelta(hours=policy().resignal_cooldown_hours)

    existing = (NotificationEvent.objects
                .filter(subject=signal.patient, dedupe_key=dedupe_key,
                        created_at__gte=window)
                .order_by('-created_at')
                .first())
    if existing is not None:
        existing.occurrence_count += 1
        existing.save(update_fields=['occurrence_count', 'updated_at'])
        return existing, False

    severity = {
        signal.Severity.INFO:      NotificationEvent.Severity.INFO,
        signal.Severity.ATTENTION: NotificationEvent.Severity.ATTENTION,
        signal.Severity.URGENT:    NotificationEvent.Severity.URGENT,
    }.get(signal.severity, NotificationEvent.Severity.ATTENTION)

    event = NotificationEvent.objects.create(
        kind=NotificationEvent.Kind.CARE_SIGNAL,
        severity=severity,
        subject=signal.patient,
        signal=signal,
        dedupe_key=dedupe_key,
    )
    return event, True


def deliver(event, *, channels=None) -> list:
    """
    Send one event to everyone entitled to it. Returns the delivery rows.

    The authorization call is not optional and not cached. It is asked fresh for
    every dispatch, so a share revoked five minutes ago stops the next message
    without anything else having to notice.
    """
    from .channels import default_channels, get_channel
    from .events import NotificationDelivery
    from .recipients import caregivers_for, render_for_caregiver

    channel_names = list(channels) if channels else default_channels()
    deliveries = []

    for recipient in caregivers_for(event.subject):
        title, body, link = render_for_caregiver(event)
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


def dispatch_signal(signal, *, channels=None) -> list:
    """Signal in, deliveries out. The whole chain, for callers that want it."""
    event, created = event_for_signal(signal)
    if not created:
        # Folded into a live event. The count moved; nobody is messaged again.
        return []
    return deliver(event, channels=channels)

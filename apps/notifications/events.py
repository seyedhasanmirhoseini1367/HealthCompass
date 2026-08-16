"""
Notification as its own concern: what happened, who may hear it, how it reaches them.

Why this is separate from the health event
------------------------------------------
The existing appointment reminder does the whole job in one place — it finds due
appointments, writes a Notification row and calls send_push, all in one loop. It
works, and it is why adding a second delivery channel or a second recipient
would mean editing the appointment code. Domain logic and delivery are welded
together.

Here they are not. A MonitoringSignal knows nothing about who is told or how. A
NotificationEvent knows what is worth saying and to whom; a NotificationDelivery
knows one attempt down one channel. Adding SMS later touches the channel
registry and nothing else, and a channel that is unavailable in this environment
reports that fact rather than silently dropping the message.

The authorization gate sits between the event and its recipients, not inside the
channel. A channel that authorised its own sends would need the rule
reimplemented once per channel, and the first one to get it wrong would leak.
"""
from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)


class NotificationEvent(models.Model):
    """
    Something worth telling someone about — independent of who or how.

    Carries a link back to the signal that caused it so that "why was I told
    this?" is answerable, and so that a notification whose evidence has been
    erased cannot keep justifying itself.

    It deliberately does NOT carry a rendered message. What a caregiver may see
    depends on what the patient shared with THAT caregiver, so the text is built
    per recipient, after the authorization check, and never before.
    """

    class Kind(models.TextChoices):
        CARE_SIGNAL      = 'care_signal',      'Care monitoring signal'
        TASK_REMINDER    = 'task_reminder',    'Task reminder'
        APPOINTMENT      = 'appointment',      'Appointment reminder'

    class Severity(models.TextChoices):
        INFO      = 'info',      'For information'
        ATTENTION = 'attention', 'Worth a look'
        URGENT    = 'urgent',    'Needs attention now'

    id      = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind    = models.CharField(max_length=32, choices=Kind.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices,
                                default=Severity.ATTENTION)

    #: Whose situation this is about. Not necessarily who is told.
    subject = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='notification_events_about')

    signal = models.ForeignKey('care.MonitoringSignal', null=True, blank=True,
                               on_delete=models.CASCADE,
                               related_name='notification_events')

    #: Stable identity for "this same thing". Two events with the same key
    #: inside the aggregation window are one event with a count, not two
    #: notifications — see `aggregate_into`.
    dedupe_key = models.CharField(max_length=200, blank=True, default='')
    #: How many times the underlying thing has recurred while this event was
    #: still the current one. Shown as "3 times", never as three notifications.
    occurrence_count = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['subject', '-created_at']),
            models.Index(fields=['dedupe_key', '-created_at']),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} about {self.subject_id} [{self.severity}]'


class NotificationDelivery(models.Model):
    """
    One attempt to reach one person down one channel.

    Separate from the event so that a failure to send an SMS does not look like
    the event never happened, and so the same event reaching a patient in-app
    and a caregiver by email is two rows with two outcomes rather than one
    ambiguous success flag.

    `body` is stored because it is what the recipient actually saw. When the
    question later is "did we disclose more than we should have", the answer has
    to come from the delivered text, not from re-rendering it under today's
    rules and today's sharing scope.
    """

    class Status(models.TextChoices):
        PENDING     = 'pending',     'Queued'
        SENT        = 'sent',        'Sent'
        FAILED      = 'failed',      'Failed'
        SUPPRESSED  = 'suppressed',  'Suppressed'
        UNAVAILABLE = 'unavailable', 'Channel not available here'

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event     = models.ForeignKey(NotificationEvent, on_delete=models.CASCADE,
                                  related_name='deliveries')
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name='notification_deliveries')
    channel   = models.CharField(max_length=32)

    title = models.CharField(max_length=200, blank=True, default='')
    body  = models.TextField(blank=True, default='')

    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PENDING)
    #: Why it did not go. Never carries provider payloads — an error string from
    #: a mail server can echo the whole message back, including its contents.
    detail  = models.CharField(max_length=200, blank=True, default='')
    sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # One attempt per (event, recipient, channel). A retry updates the
            # row rather than adding another, so a flaky channel cannot turn one
            # worry into a column of identical entries in someone's inbox.
            models.UniqueConstraint(fields=['event', 'recipient', 'channel'],
                                    name='unique_delivery_per_event_recipient_channel'),
        ]
        indexes = [
            models.Index(fields=['recipient', '-created_at']),
            models.Index(fields=['status', 'channel']),
        ]

    def __str__(self):
        return f'{self.channel} -> {self.recipient_id} [{self.status}]'

    def mark(self, status, *, detail: str = ''):
        self.status = status
        self.detail = detail[:200]
        if status == self.Status.SENT:
            self.sent_at = timezone.now()
        self.save(update_fields=['status', 'detail', 'sent_at'])
        return self

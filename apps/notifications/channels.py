"""
Delivery channels: one interface, several backends, no lies about availability.

A channel either delivers or reports why it did not. The failure mode this is
written against is the silent no-op: `send_push` currently returns quietly when
Firebase is unconfigured, so a deployment with no credentials looks identical to
one that is delivering, and the first anyone learns of it is a caregiver saying
"I never got anything".

So every channel answers `is_available()`, and an unavailable one produces a
delivery row with status UNAVAILABLE. Nothing is lost, and the gap is visible in
the database rather than inferable from an absence.

Channels this environment can actually support today
----------------------------------------------------
  in_app   yes  — a Notification row; no external dependency
  push     only when FIREBASE_CREDENTIALS_JSON is set
  email    only when SMTP is configured; otherwise Django's console backend,
           which is not delivery and says so
  sms      no   — needs a paid provider; declared so the architecture is
           complete without pretending the capability exists
  voice    no   — same, plus it must not read health information aloud to
           whoever answers the phone

In-app is the default because it is the only one that works everywhere with no
account, no credential and no cost, and the brief is explicit that the core
architecture must not be blocked on a paid provider.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class Channel:
    """One way of reaching a person. Subclasses implement `deliver`."""

    #: Stable identifier stored on NotificationDelivery.channel.
    name = ''
    #: Shown to a user choosing channels.
    label = ''

    def is_available(self) -> bool:
        """Can this deployment actually send through it right now?"""
        return False

    def deliver(self, *, recipient, title: str, body: str, event,
                link: str = '') -> tuple[bool, str]:
        """Return (sent, detail). Detail must never contain the message body."""
        raise NotImplementedError


class InAppChannel(Channel):
    """
    A row the person sees next time they open the app.

    Always available: no provider, no credential, no cost. It is also the only
    channel where the message is read inside an authenticated session, which is
    why the in-app copy can afford to be slightly more specific than a push.
    """

    name  = 'in_app'
    label = 'In the app'

    def is_available(self) -> bool:
        return True

    def deliver(self, *, recipient, title, body, event, link=''):
        from .models import Notification

        notification = Notification(
            user=recipient, type=Notification.Type.HEALTH_ALERT,
            title=title[:200], message=body, link=link or '/care/')
        # Tells the post_save receiver to stay out of it. Push is a channel of
        # this pipeline, with its own availability check and its own delivery
        # row; without this flag every in-app message would also fire an
        # unrecorded push and the recipient would be told twice.
        notification._delivered_by_pipeline = True
        notification.save()
        return True, ''


class PushChannel(Channel):
    """
    Firebase Cloud Messaging, when configured.

    Reports unavailability rather than no-opping. The existing send_push()
    already returns silently without credentials, which is what made a
    misconfigured deployment indistinguishable from a working one.
    """

    name  = 'push'
    label = 'Phone notification'

    def is_available(self) -> bool:
        return bool(getattr(settings, 'FIREBASE_CREDENTIALS_JSON', ''))

    def deliver(self, *, recipient, title, body, event, link=''):
        from .firebase import send_push
        from .models import FCMDevice

        if not FCMDevice.objects.filter(user=recipient).exists():
            # Not a failure of the channel — this person has no registered
            # device. Distinguished so nobody debugs Firebase over it.
            return False, 'recipient has no registered device'
        try:
            send_push(recipient, title, body, {'event': str(event.pk)})
        except Exception as exc:
            return False, type(exc).__name__
        return True, ''


class EmailChannel(Channel):
    """
    Email, when a real backend is configured.

    Under the console backend this is a developer convenience and not delivery,
    so it declares itself unavailable rather than recording SENT for something
    that only ever reached a terminal.

    The subject line is the title only. Mail subjects are shown in previews,
    stored by intermediate servers and indexed by clients, so it gets the same
    minimisation as a push and never the body.
    """

    name  = 'email'
    label = 'Email'

    def is_available(self) -> bool:
        backend = getattr(settings, 'EMAIL_BACKEND', '')
        return bool(backend) and 'console' not in backend and 'dummy' not in backend

    def deliver(self, *, recipient, title, body, event, link=''):
        from django.core.mail import send_mail

        address = (getattr(recipient, 'email', '') or '').strip()
        if not address:
            return False, 'recipient has no email address'
        try:
            send_mail(subject=title,
                      message=body,
                      from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                      recipient_list=[address],
                      fail_silently=False)
        except Exception as exc:
            # Type only. A mail server's error text routinely quotes the message
            # it rejected, which would put the body into the delivery row and
            # from there into any log that reads it.
            return False, type(exc).__name__
        return True, ''


class UnavailableChannel(Channel):
    """
    A channel the architecture supports and this environment cannot provide.

    Declared rather than omitted so the shape of the system is honest: adding
    SMS later means making `is_available` true and writing `deliver`, not
    redesigning the pipeline. Until then it produces UNAVAILABLE rows, which is
    a truthful record that someone would have been messaged had it existed.
    """

    def __init__(self, name, label, reason):
        self.name, self.label, self.reason = name, label, reason

    def is_available(self) -> bool:
        return False

    def deliver(self, *, recipient, title, body, event, link=''):
        return False, self.reason


SMS_CHANNEL = UnavailableChannel(
    'sms', 'Text message', 'no SMS provider configured')

#: Voice is not merely unimplemented; it carries a requirement the others do not.
#: A telephone call reaches whoever picks up the handset, which is not
#: necessarily the person the message is for. Any future implementation has to
#: establish who answered before saying anything about someone's health.
VOICE_CHANNEL = UnavailableChannel(
    'voice', 'Phone call',
    'no voice provider configured; requires recipient verification before use')


_REGISTRY = {c.name: c for c in (
    InAppChannel(), PushChannel(), EmailChannel(), SMS_CHANNEL, VOICE_CHANNEL)}


def get_channel(name: str) -> Channel | None:
    return _REGISTRY.get(name)


def available_channels() -> list:
    return [c for c in _REGISTRY.values() if c.is_available()]


def all_channels() -> list:
    return list(_REGISTRY.values())


def default_channels() -> list[str]:
    """
    What to try for a care notification, in order of preference.

    In-app is always included and always last-resort-proof: whatever else fails,
    the message is waiting when the person next opens the app. Push is attempted
    when configured because a caregiver who is not in the app is the case this
    whole feature exists for — the brief's point that family sharing must not
    require remembering to log in every day.
    """
    configured = getattr(settings, 'CARE_NOTIFICATION_CHANNELS', None)
    if configured:
        return list(configured)
    return ['in_app', 'push', 'email']

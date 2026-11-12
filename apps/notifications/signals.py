"""Push for Notification rows written directly (not through the delivery pipeline).

Module level, so the receiver lives as long as the module. It used to be a local
function inside AppConfig.ready(); signals hold receivers weakly, so it was
garbage-collected and no push was sent (audit CQ-01).
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Notification

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Notification, dispatch_uid='notifications.push_on_create')
def push_on_notification(sender, instance, created, **kwargs):
    """
    Convenience push for code that writes a Notification directly.

    Skipped for rows the delivery pipeline created: that pipeline treats push as
    its own channel with its own NotificationDelivery row, and firing here too
    would send the message twice and record neither send.
    """
    if not created or getattr(instance, '_delivered_by_pipeline', False):
        return
    try:
        from .firebase import send_push
        send_push(instance.user, instance.title, instance.message)
    except Exception as exc:
        # A push that never arrives is the failure nobody notices; log it.
        logger.warning('Push for notification %s failed: %s', instance.pk, type(exc).__name__)

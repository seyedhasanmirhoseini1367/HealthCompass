from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.notifications'
    label = 'notifications'

    def ready(self):
        import logging

        from django.db.models.signals import post_save
        from django.dispatch import receiver

        from .models import Notification

        logger = logging.getLogger(__name__)

        @receiver(post_save, sender=Notification, dispatch_uid='notifications.push_on_create')
        def push_on_notification(sender, instance, created, **kwargs):
            """
            Convenience push for code that writes a Notification directly.

            Deliberately skipped for rows the delivery pipeline created. That
            pipeline treats push as its own channel with its own availability
            check and its own NotificationDelivery row; letting this receiver
            also fire would send the message twice and record neither send,
            which is precisely the coupling the pipeline exists to remove.
            """
            if not created or getattr(instance, '_delivered_by_pipeline', False):
                return
            try:
                from .firebase import send_push
                send_push(instance.user, instance.title, instance.message)
            except Exception as exc:
                # Was `except Exception: pass`. A push that never arrives is
                # exactly the failure nobody notices, and swallowing it without
                # a word meant a broken Firebase config looked like silence.
                logger.warning('Push for notification %s failed: %s',
                               instance.pk, type(exc).__name__)

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.notifications'
    label = 'notifications'

    def ready(self):
        from django.db.models.signals import post_save
        from django.dispatch import receiver
        from .models import Notification

        @receiver(post_save, sender=Notification)
        def push_on_notification(sender, instance, created, **kwargs):
            if not created:
                return
            try:
                from .firebase import send_push
                send_push(instance.user, instance.title, instance.message)
            except Exception:
                pass

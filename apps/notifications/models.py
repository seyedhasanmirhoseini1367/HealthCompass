import uuid
from django.db import models
from django.conf import settings


class Notification(models.Model):
    class Type(models.TextChoices):
        HEALTH_ALERT  = 'health_alert',  'Health Alert'
        RECORD_PARSED = 'record_parsed', 'Record Parsed'
        MODEL_RESULT  = 'model_result',  'Model Result'
        SYSTEM        = 'system',        'System'

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user           = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                       related_name='notifications')
    type           = models.CharField(max_length=20, choices=Type.choices, default=Type.SYSTEM)
    title          = models.CharField(max_length=200)
    message        = models.TextField()
    is_read        = models.BooleanField(default=False)
    link           = models.CharField(max_length=500, blank=True, help_text='URL to navigate to on click')
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} → {self.user.username}'

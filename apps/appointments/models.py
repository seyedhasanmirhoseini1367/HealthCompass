import uuid
from django.db import models
from django.conf import settings


class Appointment(models.Model):
    id                 = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient            = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                           related_name='appointments')
    title              = models.CharField(max_length=255)
    doctor_name        = models.CharField(max_length=255, blank=True)
    location           = models.CharField(max_length=500, blank=True)
    appointment_datetime = models.DateTimeField()
    notes              = models.TextField(blank=True)

    # Which reminders the user wants
    remind_24h = models.BooleanField(default=True)
    remind_3h  = models.BooleanField(default=False)
    remind_2h  = models.BooleanField(default=False)
    remind_1h  = models.BooleanField(default=True)

    # Tracks whether each reminder has been dispatched
    reminded_24h = models.BooleanField(default=False)
    reminded_3h  = models.BooleanField(default=False)
    reminded_2h  = models.BooleanField(default=False)
    reminded_1h  = models.BooleanField(default=False)

    is_completed = models.BooleanField(default=False)
    is_cancelled = models.BooleanField(default=False)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['appointment_datetime']

    def __str__(self):
        return f'{self.title} — {self.patient.get_full_name()} @ {self.appointment_datetime:%Y-%m-%d %H:%M}'

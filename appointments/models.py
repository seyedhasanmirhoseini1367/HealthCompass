import datetime
from django.db import models
from django.conf import settings


class Appointment(models.Model):

    class Status(models.TextChoices):
        SCHEDULED   = 'scheduled',   'Scheduled'
        COMPLETED   = 'completed',   'Completed'
        CANCELLED   = 'cancelled',   'Cancelled'
        RESCHEDULED = 'rescheduled', 'Rescheduled'

    class AppointmentType(models.TextChoices):
        GENERAL    = 'general',    'General / GP'
        SPECIALIST = 'specialist', 'Specialist'
        FOLLOW_UP  = 'follow_up',  'Follow-up'
        LAB        = 'lab',        'Lab / Blood test'
        IMAGING    = 'imaging',    'Imaging / Scan'
        DENTAL     = 'dental',     'Dental'
        MENTAL     = 'mental',     'Mental health'
        PHYSIO     = 'physio',     'Physiotherapy'
        OTHER      = 'other',      'Other'

    TYPE_ICONS = {
        'general': '🩺', 'specialist': '👨‍⚕️', 'follow_up': '🔄',
        'lab': '🧪', 'imaging': '🔬', 'dental': '🦷',
        'mental': '🧠', 'physio': '💪', 'other': '📋',
    }

    patient          = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                           related_name='appointments')
    title            = models.CharField(max_length=200)
    appointment_type = models.CharField(max_length=20, choices=AppointmentType.choices,
                           default=AppointmentType.GENERAL)
    doctor_name      = models.CharField(max_length=150, blank=True)
    specialty        = models.CharField(max_length=100, blank=True)
    location         = models.CharField(max_length=300, blank=True,
                           help_text='Hospital, clinic name or address')
    date             = models.DateField()
    time             = models.TimeField(null=True, blank=True)
    duration_minutes = models.PositiveSmallIntegerField(default=30)
    status           = models.CharField(max_length=15, choices=Status.choices,
                           default=Status.SCHEDULED)
    linked_course    = models.ForeignKey('treatments.TreatmentCourse', on_delete=models.SET_NULL,
                           null=True, blank=True, related_name='appointments')
    notes            = models.TextField(blank=True,
                           help_text='Preparation notes, questions for doctor, etc.')
    outcome          = models.TextField(blank=True,
                           help_text='What was decided / next steps after the appointment')
    reminder_sent    = models.BooleanField(default=False)
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date', 'time']

    def __str__(self):
        return f'{self.title} — {self.date}'

    @property
    def is_upcoming(self):
        return self.date >= datetime.date.today() and self.status == 'scheduled'

    @property
    def is_today(self):
        return self.date == datetime.date.today()

    @property
    def is_overdue(self):
        return self.date < datetime.date.today() and self.status == 'scheduled'

    @property
    def type_icon(self):
        return self.TYPE_ICONS.get(self.appointment_type, '📋')

from django.db import models
from django.conf import settings
from django.utils import timezone
import datetime


class TreatmentCourse(models.Model):
    class Status(models.TextChoices):
        ACTIVE    = 'active',    'Active'
        COMPLETED = 'completed', 'Completed'
        PAUSED    = 'paused',    'Paused'
        CANCELLED = 'cancelled', 'Cancelled'

    class Specialty(models.TextChoices):
        DERMATOLOGY    = 'dermatology',    'Dermatology'
        CARDIOLOGY     = 'cardiology',     'Cardiology'
        ENDOCRINOLOGY  = 'endocrinology',  'Endocrinology'
        NEUROLOGY      = 'neurology',      'Neurology'
        GASTROENTERO   = 'gastro',         'Gastroenterology'
        ORTHOPEDICS    = 'orthopedics',    'Orthopedics'
        PULMONOLOGY    = 'pulmonology',    'Pulmonology'
        RHEUMATOLOGY   = 'rheumatology',   'Rheumatology'
        ONCOLOGY       = 'oncology',       'Oncology'
        PSYCHIATRY     = 'psychiatry',     'Psychiatry'
        GENERAL        = 'general',        'General / Family'
        OTHER          = 'other',          'Other'

    patient           = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                            related_name='treatment_courses')
    name              = models.CharField(max_length=200,
                            help_text='Short name, e.g. "Acne Treatment Course"')
    condition         = models.CharField(max_length=200,
                            help_text='Medical condition being treated')
    specialty         = models.CharField(max_length=30, choices=Specialty.choices,
                            default=Specialty.OTHER)
    doctor_name       = models.CharField(max_length=150, blank=True)
    start_date        = models.DateField()
    expected_end_date = models.DateField(null=True, blank=True,
                            help_text='Estimated end of treatment (optional)')
    status            = models.CharField(max_length=15, choices=Status.choices,
                            default=Status.ACTIVE)
    medications       = models.TextField(blank=True,
                            help_text='Medications used in this course (free text)')
    notes             = models.TextField(blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f'{self.name} ({self.patient.username})'

    @property
    def days_elapsed(self):
        return (timezone.now().date() - self.start_date).days

    @property
    def total_days(self):
        if self.expected_end_date:
            return max((self.expected_end_date - self.start_date).days, 1)
        return None

    @property
    def progress_pct(self):
        if self.total_days:
            return min(round(self.days_elapsed / self.total_days * 100), 100)
        return None

    @property
    def is_overdue(self):
        if self.expected_end_date and self.status == self.Status.ACTIVE:
            return timezone.now().date() > self.expected_end_date
        return False


class TreatmentMilestone(models.Model):
    class MilestoneType(models.TextChoices):
        VISIT             = 'visit',       'Doctor Visit'
        LAB_TEST          = 'lab_test',    'Lab Test'
        MEDICATION_CHANGE = 'med_change',  'Medication Change'
        SYMPTOM_NOTE      = 'symptom',     'Symptom / Side Effect'
        PHOTO             = 'photo',       'Progress Photo Note'
        OTHER             = 'other',       'Other'

    class Outcome(models.TextChoices):
        IMPROVED = 'improved', 'Improved'
        STABLE   = 'stable',   'Stable'
        WORSENED = 'worsened', 'Worsened'
        PENDING  = 'pending',  'Pending / Unknown'

    course         = models.ForeignKey(TreatmentCourse, on_delete=models.CASCADE,
                         related_name='milestones')
    date           = models.DateField(default=datetime.date.today)
    title          = models.CharField(max_length=200)
    milestone_type = models.CharField(max_length=20, choices=MilestoneType.choices,
                         default=MilestoneType.OTHER)
    outcome        = models.CharField(max_length=15, choices=Outcome.choices,
                         default=Outcome.PENDING)
    note           = models.TextField(blank=True)
    linked_record  = models.ForeignKey('medical_records.MedicalRecord',
                         on_delete=models.SET_NULL, null=True, blank=True,
                         related_name='treatment_milestones',
                         help_text='Link to an uploaded lab/record if applicable')
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.title} — {self.date}'


class CourseMonitor(models.Model):
    """A biomarker to watch during this course, with a check frequency."""
    course          = models.ForeignKey(TreatmentCourse, on_delete=models.CASCADE,
                          related_name='monitors')
    biomarker_name  = models.CharField(max_length=100,
                          help_text='e.g. "ALT (Liver)", "Triglycerides"')
    frequency_days  = models.PositiveIntegerField(
                          help_text='How often to test this (in days), e.g. 30')
    last_checked    = models.DateField(null=True, blank=True)
    note            = models.CharField(max_length=200, blank=True,
                          help_text='Why this is being monitored')
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['biomarker_name']

    def __str__(self):
        return f'{self.biomarker_name} every {self.frequency_days}d'

    @property
    def next_due(self):
        base = self.last_checked or self.course.start_date
        return base + datetime.timedelta(days=self.frequency_days)

    @property
    def days_until_due(self):
        return (self.next_due - timezone.now().date()).days

    @property
    def is_due(self):
        return self.days_until_due <= 0

    @property
    def overdue_days(self):
        """How many days past due (always non-negative)."""
        return abs(self.days_until_due) if self.is_due else 0

    @property
    def is_due_soon(self):
        """Due within the next 7 days."""
        return 0 < self.days_until_due <= 7

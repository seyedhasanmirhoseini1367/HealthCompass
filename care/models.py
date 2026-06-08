import uuid
import datetime
from django.db import models
from django.conf import settings
from django.utils import timezone


class CareCircle(models.Model):
    """One per patient — their remote care setup."""
    patient    = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                     related_name='care_circle')
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # ── Escalation schedule ──────────────────────────────────────────────────
    usual_checkin_hour   = models.PositiveSmallIntegerField(
        default=9,
        help_text='Hour of day (0–23, server timezone) when patient usually checks in'
    )
    checkin_window_hours = models.PositiveSmallIntegerField(
        default=2,
        help_text='Grace period in hours after usual check-in time before first alert'
    )
    daily_summary_enabled = models.BooleanField(
        default=True,
        help_text='Send a daily summary email to all caregivers at 8 pm'
    )

    # ── Passive heartbeat ───────────────────────────────────────────────────
    last_active_at = models.DateTimeField(null=True, blank=True,
        help_text='Last time the patient visited any Care page (passive signal)')

    # ── Quick "I'm fine" token ───────────────────────────────────────────────
    quick_token = models.UUIDField(
        default=uuid.uuid4, unique=True, editable=False,
        help_text='Token for no-login one-tap "I am fine" check-in link'
    )

    def __str__(self):
        return f'Care Circle: {self.patient.get_full_name() or self.patient.username}'

    @property
    def active_caregivers(self):
        return self.caregivers.filter(is_active=True)


class CareGiver(models.Model):
    """A family member or nurse who monitors the patient remotely."""

    class Relationship(models.TextChoices):
        SON      = 'son',      'Son'
        DAUGHTER = 'daughter', 'Daughter'
        SPOUSE   = 'spouse',   'Spouse / Partner'
        PARENT   = 'parent',   'Parent'
        SIBLING  = 'sibling',  'Sibling'
        NURSE    = 'nurse',    'Nurse / Caregiver'
        DOCTOR   = 'doctor',   'Doctor'
        FRIEND   = 'friend',   'Friend'
        OTHER    = 'other',    'Other'

    circle        = models.ForeignKey(CareCircle, on_delete=models.CASCADE,
                        related_name='caregivers')
    name          = models.CharField(max_length=150)
    email         = models.EmailField(blank=True)
    relationship  = models.CharField(max_length=20, choices=Relationship.choices,
                        default=Relationship.OTHER)
    access_token  = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_active     = models.BooleanField(default=True)
    last_viewed   = models.DateTimeField(null=True, blank=True)
    invited_at    = models.DateTimeField(auto_now_add=True)

    # ── Notification preferences ─────────────────────────────────────────────
    phone_number     = models.CharField(max_length=30, blank=True,
                           help_text='Include country code, e.g. +358501234567')
    notify_email     = models.BooleanField(default=True,
                           help_text='Send email alerts for missed check-ins (Tier 2+)')
    notify_sms       = models.BooleanField(default=False,
                           help_text='Send SMS alerts (requires phone_number and Twilio config)')
    notify_priority  = models.PositiveSmallIntegerField(default=1,
                           help_text='Lower = notified first when multiple caregivers exist')

    # ── Do-not-disturb (server timezone, 0–23) ───────────────────────────────
    quiet_start = models.PositiveSmallIntegerField(null=True, blank=True,
                      help_text='Quiet hours start (0–23). Leave blank to disable.')
    quiet_end   = models.PositiveSmallIntegerField(null=True, blank=True,
                      help_text='Quiet hours end (0–23). Wraps midnight correctly.')

    class Meta:
        ordering = ['notify_priority', 'name']

    def __str__(self):
        return f'{self.name} ({self.get_relationship_display()})'

    def in_quiet_hours(self, now=None):
        """Return True if `now` falls inside this caregiver's quiet window."""
        if self.quiet_start is None or self.quiet_end is None:
            return False
        if now is None:
            now = timezone.localtime(timezone.now())
        h = now.hour
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= h < self.quiet_end
        else:  # window wraps midnight e.g. 22→07
            return h >= self.quiet_start or h < self.quiet_end


class DailyCheckIn(models.Model):
    """Patient's daily self-report."""

    MOOD_CHOICES = [
        ('great', '😄 Great'),
        ('good',  '🙂 Good'),
        ('okay',  '😐 Okay'),
        ('low',   '😔 Low'),
        ('bad',   '😟 Bad'),
    ]

    patient           = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                            related_name='daily_checkins')
    date              = models.DateField(default=datetime.date.today)
    feeling_score     = models.PositiveSmallIntegerField(null=True, blank=True,
                            help_text='Overall feeling 1–10')
    pain_score        = models.PositiveSmallIntegerField(null=True, blank=True,
                            help_text='Pain level 0–10 (0 = no pain)')
    sleep_score       = models.PositiveSmallIntegerField(null=True, blank=True,
                            help_text='Sleep quality 1–10')
    mood              = models.CharField(max_length=10, choices=MOOD_CHOICES, blank=True)
    symptoms          = models.TextField(blank=True,
                            help_text='Any symptoms, concerns, or free notes')
    medications_taken = models.BooleanField(null=True, blank=True)
    is_flagged        = models.BooleanField(default=False)
    flag_reason       = models.TextField(blank=True)
    is_quick          = models.BooleanField(default=False,
                            help_text='Quick "I am fine" tap — no detailed scores')
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        unique_together = [('patient', 'date')]

    def __str__(self):
        label = ' [quick]' if self.is_quick else ''
        return f'Check-in {self.patient.username} {self.date}{label}'

    def auto_flag(self):
        """Detect concerning entries and set is_flagged + flag_reason."""
        reasons = []
        if self.feeling_score and self.feeling_score <= 3:
            reasons.append(f'Very low feeling score ({self.feeling_score}/10)')
        if self.pain_score and self.pain_score >= 7:
            reasons.append(f'High pain level ({self.pain_score}/10)')
        if self.sleep_score and self.sleep_score <= 2:
            reasons.append(f'Very poor sleep ({self.sleep_score}/10)')
        if self.mood == 'bad':
            reasons.append('Mood reported as very bad')
        urgent = [
            'emergency', 'hospital', 'ambulance', 'chest pain',
            "can't breathe", 'fell down', 'fallen', 'fainted',
            'unconscious', 'help me', 'bleeding', 'very bad',
        ]
        if self.symptoms:
            low = self.symptoms.lower()
            for word in urgent:
                if word in low:
                    reasons.append(f'Urgent keyword detected: "{word}"')
                    break
        self.is_flagged  = bool(reasons)
        self.flag_reason = ' · '.join(reasons)


class MedicationSchedule(models.Model):
    """A medication the patient takes regularly."""

    class TimeOfDay(models.TextChoices):
        MORNING   = 'morning',   '🌅 Morning'
        AFTERNOON = 'afternoon', '☀️ Afternoon'
        EVENING   = 'evening',   '🌆 Evening'
        NIGHT     = 'night',     '🌙 Night (before bed)'
        WITH_MEAL = 'meal',      '🍽 With meal'

    patient        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                         related_name='medication_schedules')
    name           = models.CharField(max_length=200)
    dosage         = models.CharField(max_length=100, blank=True, help_text='e.g. 20 mg')
    time_of_day    = models.CharField(max_length=15, choices=TimeOfDay.choices,
                         default=TimeOfDay.MORNING)
    linked_course  = models.ForeignKey('treatments.TreatmentCourse', on_delete=models.SET_NULL,
                         null=True, blank=True, related_name='care_medications',
                         help_text='Optional link to a treatment course')
    notes          = models.CharField(max_length=300, blank=True)
    is_active      = models.BooleanField(default=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['time_of_day', 'name']

    def __str__(self):
        return f'{self.name} {self.dosage} ({self.get_time_of_day_display()})'


class MedicationLog(models.Model):
    """Whether a medication was taken on a given date."""
    schedule  = models.ForeignKey(MedicationSchedule, on_delete=models.CASCADE,
                    related_name='logs')
    date      = models.DateField(default=datetime.date.today)
    taken     = models.BooleanField()
    note      = models.CharField(max_length=200, blank=True)
    logged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        unique_together = [('schedule', 'date')]

    def __str__(self):
        return f'{self.schedule.name} {"✓" if self.taken else "✗"} {self.date}'


class PainLog(models.Model):
    """Granular pain entry — body location, type, severity, trigger."""

    BODY_AREAS = [
        ('head',       'Head'),
        ('neck',       'Neck / Throat'),
        ('chest',      'Chest'),
        ('back_upper', 'Upper Back'),
        ('back_lower', 'Lower Back'),
        ('abdomen',    'Abdomen'),
        ('arm_left',   'Left Arm / Shoulder'),
        ('arm_right',  'Right Arm / Shoulder'),
        ('leg_left',   'Left Leg / Hip'),
        ('leg_right',  'Right Leg / Hip'),
        ('other',      'Other'),
    ]

    PAIN_TYPES = [
        ('sharp',     'Sharp'),
        ('dull',      'Dull / Aching'),
        ('burning',   'Burning'),
        ('throbbing', 'Throbbing'),
        ('stabbing',  'Stabbing'),
        ('cramping',  'Cramping'),
        ('pressure',  'Pressure / Tightness'),
        ('other',     'Other'),
    ]

    patient    = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                     related_name='pain_logs')
    date       = models.DateField(default=datetime.date.today)
    body_area  = models.CharField(max_length=20, choices=BODY_AREAS)
    pain_type  = models.CharField(max_length=15, choices=PAIN_TYPES, blank=True)
    severity   = models.PositiveSmallIntegerField(help_text='1 (mild) – 10 (severe)')
    trigger    = models.CharField(max_length=200, blank=True,
                     help_text='What triggered or worsened the pain?')
    notes      = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f'{self.get_body_area_display()} pain {self.severity}/10 on {self.date}'


class EscalationLog(models.Model):
    """
    Record of every escalation notification sent (or attempted) for a missed check-in.
    One record per tier × patient × date × caregiver.

    Tier guide:
      2 → email only (6 h overdue)
      3 → email + SMS (12 h overdue)
      4 → email + SMS, urgent subject (24 h overdue)
    """

    CHANNEL_CHOICES = [
        ('email',         'Email alert'),
        ('sms',           'SMS alert'),
        ('summary_email', 'Daily summary email'),
    ]

    TIER_LABELS = {
        2: 'Caregiver notified — 6 h overdue',
        3: 'Attention — 12 h overdue (email + SMS)',
        4: 'URGENT — 24 h+ overdue',
    }

    patient           = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                            related_name='escalation_logs')
    caregiver         = models.ForeignKey(CareGiver, on_delete=models.SET_NULL,
                            null=True, blank=True, related_name='escalation_logs')
    tier              = models.PositiveSmallIntegerField(help_text='Escalation tier (2–4)')
    channel           = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    date              = models.DateField(help_text='Which check-in date this alert is for')
    sent_at           = models.DateTimeField(auto_now_add=True)
    acknowledged_at   = models.DateTimeField(null=True, blank=True)
    acknowledged_by   = models.CharField(max_length=150, blank=True)
    acknowledge_token = models.UUIDField(default=uuid.uuid4, unique=True,
                            help_text='Used in one-click email acknowledge links')
    is_dismissed      = models.BooleanField(default=False)

    class Meta:
        ordering = ['-sent_at']
        indexes = [
            models.Index(fields=['patient', 'date']),
            models.Index(fields=['acknowledge_token']),
        ]

    def __str__(self):
        return f'Tier-{self.tier} {self.channel} for {self.patient} on {self.date}'

    @property
    def is_acknowledged(self):
        return self.acknowledged_at is not None or self.is_dismissed

    @property
    def tier_label(self):
        return self.TIER_LABELS.get(self.tier, f'Tier {self.tier}')

    def acknowledge(self, by=''):
        if not self.acknowledged_at:
            self.acknowledged_at = timezone.now()
            self.acknowledged_by = by
            self.save(update_fields=['acknowledged_at', 'acknowledged_by'])

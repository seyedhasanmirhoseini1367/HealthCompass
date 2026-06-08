import datetime
from django.db import models
from django.conf import settings


class HealthGoal(models.Model):

    class GoalType(models.TextChoices):
        WEIGHT      = 'weight',      'Body Weight (kg)'
        BLOOD_PRESS = 'bp',          'Blood Pressure (mmHg)'
        STEPS       = 'steps',       'Daily Steps'
        SLEEP       = 'sleep',       'Sleep (hours/night)'
        PAIN        = 'pain',        'Pain Level (0–10)'
        FEELING     = 'feeling',     'Overall Feeling (1–10)'
        MEDICATION  = 'medication',  'Medication Adherence (%)'
        CUSTOM      = 'custom',      'Custom'

    class Status(models.TextChoices):
        ACTIVE    = 'active',    'Active'
        ACHIEVED  = 'achieved',  'Achieved'
        PAUSED    = 'paused',    'Paused'
        ABANDONED = 'abandoned', 'Abandoned'

    GOAL_ICONS = {
        'weight': '⚖️', 'bp': '❤️', 'steps': '👟',
        'sleep': '😴', 'pain': '💊', 'feeling': '😊',
        'medication': '💉', 'custom': '🎯',
    }

    patient      = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                       related_name='health_goals')
    title        = models.CharField(max_length=200)
    goal_type    = models.CharField(max_length=15, choices=GoalType.choices,
                       default=GoalType.CUSTOM)
    description  = models.TextField(blank=True)
    target_value = models.FloatField(help_text='Numeric target (e.g. 75 for 75 kg)')
    unit         = models.CharField(max_length=30, blank=True,
                       help_text='e.g. kg, steps, hours')
    start_value  = models.FloatField(null=True, blank=True,
                       help_text='Starting value when goal was created')
    start_date   = models.DateField(default=datetime.date.today)
    target_date  = models.DateField(null=True, blank=True)
    status       = models.CharField(max_length=15, choices=Status.choices,
                       default=Status.ACTIVE)
    # Lower is better flag (e.g. pain, weight-loss)
    lower_is_better = models.BooleanField(default=False)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} (target {self.target_value} {self.unit})'

    @property
    def goal_icon(self):
        return self.GOAL_ICONS.get(self.goal_type, '🎯')

    @property
    def latest_entry(self):
        return self.entries.order_by('-date').first()

    @property
    def progress_pct(self):
        if self.start_value is None or not self.entries.exists():
            return 0
        latest = self.latest_entry
        if latest is None:
            return 0
        total_change = abs(self.target_value - self.start_value)
        if total_change == 0:
            return 100
        actual_change = abs(latest.value - self.start_value)
        return min(100, round(actual_change / total_change * 100))

    @property
    def days_remaining(self):
        if self.target_date is None:
            return None
        delta = self.target_date - datetime.date.today()
        return delta.days

    @property
    def is_on_track(self):
        """Simple heuristic: progress >= expected based on days elapsed."""
        if self.target_date is None or self.start_date is None:
            return None
        total_days = (self.target_date - self.start_date).days
        elapsed    = (datetime.date.today() - self.start_date).days
        if total_days <= 0:
            return None
        expected_pct = min(100, elapsed / total_days * 100)
        return self.progress_pct >= expected_pct * 0.9  # 10% tolerance


class GoalEntry(models.Model):
    """A single data point logged toward a goal."""
    goal       = models.ForeignKey(HealthGoal, on_delete=models.CASCADE,
                     related_name='entries')
    date       = models.DateField(default=datetime.date.today)
    value      = models.FloatField()
    note       = models.CharField(max_length=300, blank=True)
    logged_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date']
        unique_together = [('goal', 'date')]

    def __str__(self):
        return f'{self.goal.title}: {self.value} on {self.date}'

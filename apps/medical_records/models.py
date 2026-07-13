import uuid
from django.db import models
from django.conf import settings


class MedicalRecord(models.Model):
    class RecordType(models.TextChoices):
        LAB_RESULT   = 'lab_result',   'Lab Result'
        PRESCRIPTION = 'prescription', 'Prescription'
        DIAGNOSIS    = 'diagnosis',    'Diagnosis'
        VACCINATION  = 'vaccination',  'Vaccination'
        IMAGING      = 'imaging',      'Imaging Report'
        WEARABLE     = 'wearable',     'Wearable Data'
        DISCHARGE    = 'discharge',    'Discharge Summary'
        OTHER        = 'other',        'Other'

    class Source(models.TextChoices):
        KANTA_XML     = 'kanta_xml',     'Kanta XML Import'
        WEARABLE_CSV  = 'wearable_csv',  'Wearable Import'
        MANUAL_UPLOAD = 'manual_upload', 'Manual Upload'

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                    related_name='medical_records')
    record_type = models.CharField(max_length=20, choices=RecordType.choices, default=RecordType.OTHER)
    source      = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL_UPLOAD)
    title       = models.CharField(max_length=255)
    file        = models.FileField(upload_to='medical_records/%Y/%m/', blank=True, null=True)
    raw_text    = models.TextField(blank=True, help_text='Extracted text from file')
    parsed_data = models.JSONField(default=dict, blank=True, help_text='Structured parsed data')
    notes       = models.TextField(blank=True)
    record_date = models.DateField(null=True, blank=True, help_text='Date the record was created/measured')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    is_flagged  = models.BooleanField(default=False, help_text='Flagged by AI as having abnormal values')

    class Meta:
        ordering = ['-record_date', '-uploaded_at']

    def __str__(self):
        return f'{self.title} ({self.get_record_type_display()}) - {self.patient.username}'


class ParsedLabValue(models.Model):
    """Individual values extracted from lab results."""
    record          = models.ForeignKey(MedicalRecord, on_delete=models.CASCADE, related_name='lab_values')
    parameter_name  = models.CharField(max_length=200)
    value           = models.CharField(max_length=100)
    unit            = models.CharField(max_length=50, blank=True)
    # Canonical value normalized to a consistent unit (e.g. mg/dL) for safe
    # cross-record comparison. Finnish labs report in SI (µmol/L, mmol/L);
    # without normalization trajectory thresholds would be wildly wrong.
    canonical_value = models.FloatField(null=True, blank=True)
    original_unit   = models.CharField(max_length=50, blank=True)
    reference_range = models.CharField(max_length=100, blank=True)
    is_abnormal     = models.BooleanField(default=False)
    is_critical     = models.BooleanField(default=False)
    measured_at     = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        flag = ' [!]' if self.is_critical else (' [abnormal]' if self.is_abnormal else '')
        return f'{self.parameter_name}: {self.value} {self.unit}{flag}'


class WearableDataPoint(models.Model):
    """Individual data points from wearable device CSV imports."""
    class Metric(models.TextChoices):
        HEART_RATE   = 'heart_rate',   'Heart Rate (bpm)'
        STEPS        = 'steps',        'Steps'
        SLEEP        = 'sleep',        'Sleep (hours)'
        CALORIES     = 'calories',     'Calories Burned'
        BLOOD_OXYGEN = 'blood_oxygen', 'Blood Oxygen (%)'
        WEIGHT       = 'weight',       'Weight (kg)'
        TEMPERATURE  = 'temperature',  'Body Temperature'
        OTHER        = 'other',        'Other'

    record      = models.ForeignKey(MedicalRecord, on_delete=models.CASCADE, related_name='wearable_points')
    metric      = models.CharField(max_length=20, choices=Metric.choices, default=Metric.OTHER)
    value       = models.FloatField()
    unit        = models.CharField(max_length=20, blank=True)
    recorded_at = models.DateTimeField()

    class Meta:
        ordering = ['recorded_at']

    def __str__(self):
        return f'{self.get_metric_display()}: {self.value} {self.unit} @ {self.recorded_at}'

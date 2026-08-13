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

    # Identity of the ingested artifact, for idempotent upload.
    #
    # Re-uploading the same document used to create a second MedicalRecord and a
    # second full set of ParsedLabValue rows. conflict_service then reported the
    # result to the patient as a `duplicate` clinical finding — the system
    # describing its own ingestion defect as a data conflict — and every
    # trajectory calculation counted the reading twice.
    #
    # Scope is deliberately the ARTIFACT, not the facts. Two blood draws that
    # happen to carry identical values on different dates are two real events and
    # must both survive; only the same bytes arriving twice is a duplicate.
    #
    # Blank for rows created before this field existed and for any path that
    # cannot compute a stable fingerprint. The uniqueness constraint below is
    # partial for exactly that reason, so historical rows are never invalidated.
    content_hash = models.CharField(
        max_length=64, blank=True, default='', db_index=True,
        help_text='SHA-256 of the ingested content. Empty when unknown '
                  '(pre-existing rows); such rows are exempt from de-duplication.')

    class Meta:
        ordering = ['-record_date', '-uploaded_at']
        indexes = [
            models.Index(fields=['patient', '-record_date']),
        ]
        constraints = [
            # The database-level guarantee. The application checks first for a
            # graceful response, but two concurrent identical uploads would both
            # pass that check; this is what actually prevents the second row.
            models.UniqueConstraint(
                fields=['patient', 'content_hash'],
                condition=~models.Q(content_hash=''),
                name='unique_record_content_per_patient',
            ),
        ]

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
    # False when the analyte is known but the incoming unit was not recognised.
    # Such rows are excluded from trajectory and threshold comparisons.
    unit_known      = models.BooleanField(default=True)
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

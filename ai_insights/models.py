import uuid
from django.db import models
from django.conf import settings


class AIModel(models.Model):
    class Status(models.TextChoices):
        PENDING  = 'pending',  'Pending Review'
        APPROVED = 'approved', 'Approved'
        ACTIVE   = 'active',   'Active'
        REJECTED = 'rejected', 'Rejected'

    class Category(models.TextChoices):
        CARDIOVASCULAR = 'cardiovascular', 'Cardiovascular'
        DIABETES       = 'diabetes',       'Diabetes'
        ONCOLOGY       = 'oncology',       'Oncology'
        NEUROLOGY      = 'neurology',      'Neurology'
        GENERAL        = 'general',        'General Health'
        WEARABLE       = 'wearable',       'Wearable Analytics'
        OTHER          = 'other',          'Other'

    class InputType(models.TextChoices):
        TABULAR  = 'tabular',  'Tabular (manual form)'
        IMAGE    = 'image',    'Image (JPG/PNG/DICOM)'
        EEG_CSV  = 'eeg_csv',  'EEG / Signal (CSV)'
        PARQUET  = 'parquet',  'Tabular file (Parquet/CSV upload)'
        AUDIO    = 'audio',    'Audio file'
        FILE     = 'file',     'Generic file'

    id                   = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    data_scientist       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='submitted_models')
    name                 = models.CharField(max_length=200)
    slug                 = models.SlugField(max_length=220, unique=True, blank=True)
    description          = models.TextField()
    category             = models.CharField(max_length=30, choices=Category.choices, default=Category.GENERAL)
    input_type           = models.CharField(max_length=20, choices=InputType.choices,
                             default=InputType.TABULAR,
                             help_text='How the patient provides input data')
    model_file           = models.FileField(upload_to='ai_models/', blank=True, null=True,
                             help_text='Upload .pkl or .h5 model file')
    input_schema         = models.JSONField(default=dict,
                             help_text='JSON definition of required input fields (for tabular input)')
    output_schema        = models.JSONField(default=dict,
                             help_text='JSON definition of model output/interpretation')
    interpretation_guide = models.TextField(blank=True,
                             help_text=(
                                 'Plain-text guide for AI interpretation. Describe the disease/condition, '
                                 'what the risk levels mean, and what actions the patient should take. '
                                 'Gemini will use this to generate a personalised explanation after each prediction.'
                             ))
    handler_slug         = models.CharField(max_length=80, blank=True,
                             help_text=(
                                 'Inference handler slug (e.g. "eeg_csv", "image_classifier", '
                                 '"tabular_passthrough"). Leave blank to use the generic runner.'
                             ))
    handler_config       = models.JSONField(default=dict, blank=True,
                             help_text=(
                                 'JSON config passed to the handler (sampling_rate_hz, label_map, etc.). '
                                 'See ADMIN_CONFIGS.md in ai_insights/inference/ for examples.'
                             ))
    status               = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    reviewed_by          = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name='reviewed_models')
    reviewed_at          = models.DateTimeField(null=True, blank=True)
    created_at           = models.DateTimeField(auto_now_add=True)
    updated_at           = models.DateTimeField(auto_now=True)
    run_count            = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} by {self.data_scientist.username} [{self.status}]'

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class ModelPrediction(models.Model):
    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    model          = models.ForeignKey(AIModel, on_delete=models.CASCADE, related_name='predictions')
    patient        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                       related_name='predictions')
    input_data     = models.JSONField(default=dict)
    input_file     = models.FileField(upload_to='prediction_inputs/', blank=True, null=True,
                       help_text='Uploaded file used as input (image, EEG, etc.)')
    result         = models.JSONField(default=dict)
    risk_score     = models.FloatField(null=True, blank=True, help_text='0.0 - 1.0 risk score if applicable')
    interpretation = models.TextField(blank=True,
                       help_text='AI-generated patient-friendly interpretation of the result')
    notes          = models.TextField(blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.model.name} prediction for {self.patient.username}'


class HealthAlert(models.Model):
    class Severity(models.TextChoices):
        INFO     = 'info',     'Info'
        WARNING  = 'warning',  'Warning'
        CRITICAL = 'critical', 'Critical'

    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient          = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                         related_name='health_alerts')
    severity         = models.CharField(max_length=10, choices=Severity.choices, default=Severity.INFO)
    title            = models.CharField(max_length=200)
    message          = models.TextField()
    source_record    = models.ForeignKey('medical_records.MedicalRecord', null=True, blank=True,
                         on_delete=models.SET_NULL, related_name='alerts')
    is_read          = models.BooleanField(default=False)
    notified_doctor  = models.BooleanField(default=False)
    notified_by_email = models.BooleanField(default=False)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.severity.upper()}] {self.title} — {self.patient.username}'

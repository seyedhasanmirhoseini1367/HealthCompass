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
    # SET_NULL, not CASCADE.
    #
    # The seizure integration used to pick its owner with
    # `User.objects.filter(is_staff=True).first()`, so an administrative flag
    # decided data ownership. Combined with CASCADE here and CASCADE on
    # ModelPrediction.model, deleting that staff account destroyed the model AND
    # every patient's seizure prediction history with it — reachable through the
    # admin's reject action, a bulk delete, or that admin's own GDPR erasure.
    #
    # Nulling the owner keeps the model and its predictions while still letting
    # the account be erased, which is what both obligations require at once.
    # PROTECT would have preserved the data by making erasure impossible, and
    # erasure is not optional.
    data_scientist       = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='submitted_models',
                             help_text='The data scientist who submitted this model. '
                                       'NULL for system-provisioned models, and for '
                                       'models whose submitter has been erased.')
    # Distinguishes "provisioned by the platform" from "submitter was erased",
    # which a NULL owner alone cannot express. Set at creation and never by a
    # user action, so it cannot be used to adopt or disown a model.
    is_system            = models.BooleanField(default=False,
                             help_text='Provisioned by the platform rather than '
                                       'submitted by a data scientist.')
    name                 = models.CharField(max_length=200)
    slug                 = models.SlugField(max_length=220, unique=True, blank=True)
    description          = models.TextField()
    category             = models.CharField(max_length=30, choices=Category.choices, default=Category.GENERAL)
    input_type           = models.CharField(max_length=20, choices=InputType.choices,
                             default=InputType.TABULAR,
                             help_text='How the patient provides input data')
    model_file           = models.FileField(upload_to='ai_models/', blank=True, null=True,
                             help_text='Upload a .onnx model file. Use convert_to_onnx.py to convert from PyTorch/Keras/sklearn.')
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
    # ── Provenance ───────────────────────────────────────────────────────────
    #
    # A data scientist can replace model_file on an existing row. Every past
    # prediction then appears to have come from the new artifact, and there was
    # no way to tell which weights actually produced a result a patient was
    # shown. version and the file digest are copied onto each ModelPrediction
    # when it is made, so a result stays traceable to what produced it.
    version              = models.CharField(max_length=50, default='1', blank=True,
                             help_text='Version of this model. Change it whenever the '
                                       'model file changes; past predictions keep the '
                                       'version they were made with.')
    model_file_sha256    = models.CharField(max_length=64, blank=True, default='',
                             help_text='SHA-256 of the uploaded model file, computed on '
                                       'save. Identifies the exact artifact.')
    intended_use         = models.TextField(blank=True,
                             help_text=(
                                 'What this model is for, which population it was '
                                 'validated on, and what it must NOT be used for. '
                                 'Shown to reviewers; a model without this cannot be '
                                 'assessed for whether it fits the patient in front of you.'
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
        owner = (self.data_scientist.username if self.data_scientist_id
                 else ('system' if self.is_system else 'no owner'))
        return f'{self.name} by {owner} [{self.status}]'

    #: The only way into ACTIVE. Activation is what makes a model patient-facing,
    #: so it is the one transition that must be earned rather than assigned.
    #:
    #: Deliberately narrow: every other transition the four states allow is left
    #: alone. This is not a general state machine, it is one gate on one edge.
    ACTIVATION_REQUIRES = 'approved'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Remembered so the digest is recomputed only when the file actually
        # changes. Guarded because a deferred queryset (.only(...)) would raise
        # here, and a row loaded for one field must still be saveable.
        try:
            self._loaded_model_file = self.model_file.name if self.model_file else ''
        except Exception:
            self._loaded_model_file = None
        # The status this row had when it was read, so save() can tell an
        # activation from a row that was already active.
        #
        # NOT guarded on self.pk: `id` is a UUIDField with a default, so a
        # brand-new instance already has one and this would record its INTENDED
        # status as its loaded status — letting a model be created straight into
        # ACTIVE. Creation is detected by `_state.adding` at save time instead,
        # which is only reliable there (Django clears it after __init__ when
        # loading from the database, not before).
        try:
            self._loaded_status = self.status
        except Exception:
            self._loaded_status = None

    def _check_activation(self):
        """
        Refuse to become ACTIVE from anywhere except APPROVED.

        Activation used to be assignable from any state and at creation time, so
        a model that had never been reviewed could be serving patients: the
        admin's activate action updated any selection straight to active, and
        the seizure integration created its model row with status=ACTIVE on the
        first analysis anyone ran.

        Enforced in save() rather than only in clean() because save() is what
        every path actually calls — the admin change form, the changelist's
        inline status editor, and any code doing obj.status = ...; obj.save().
        full_clean() is not called by save(), so a clean()-only rule would be
        advisory.

        NOT enforced against queryset.update(), which bypasses model save() by
        design. There is exactly one such call in the codebase (the admin
        activate action) and it filters on the approved state itself; a test
        asserts no second one appears.
        """
        from django.core.exceptions import ValidationError

        if self.status != self.Status.ACTIVE:
            return

        creating = self._state.adding
        if not creating and self._loaded_status == self.Status.ACTIVE:
            return          # already active; saving other fields is fine

        if creating or self._loaded_status != self.ACTIVATION_REQUIRES:
            was = 'a new record' if creating else self._loaded_status
            raise ValidationError({
                'status': (
                    f'A model can only be activated from "{self.ACTIVATION_REQUIRES}"; '
                    f'this one is {was}. Approve it first — activation is what makes '
                    f'it visible to patients.'
                )
            })

    def clean(self):
        # For the admin form and anything calling full_clean(): the same rule,
        # surfaced as a field error instead of an exception page.
        super().clean()
        self._check_activation()

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)
        self._check_activation()
        self._refresh_file_digest()
        super().save(*args, **kwargs)
        self._loaded_model_file = self.model_file.name if self.model_file else ''
        self._loaded_status = self.status

    def _refresh_file_digest(self):
        """
        Keep model_file_sha256 in step with model_file.

        Read in chunks: an ONNX file is not small. Skipped entirely when the
        file has not changed, so bumping run_count does not re-read 200 MB. A
        read failure clears the digest rather than leaving a stale one —
        claiming to identify an artifact we could not read is worse than
        admitting we do not know.
        """
        import hashlib

        if not self.model_file:
            self.model_file_sha256 = ''
            return

        current = self.model_file.name
        unchanged = (self._loaded_model_file is not None
                     and current == self._loaded_model_file
                     and self.model_file_sha256)
        if unchanged:
            return

        try:
            digest = hashlib.sha256()
            with self.model_file.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
            self.model_file_sha256 = digest.hexdigest()
        except Exception:
            self.model_file_sha256 = ''


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
    # Which artifact produced this result, captured when the prediction is made.
    #
    # The FK alone is not provenance: replacing model_file on the AIModel row
    # silently rewrites the apparent origin of every prediction ever made with
    # it. A clinical result must stay attributable to the weights that produced
    # it, including after the model is updated or withdrawn.
    model_version  = models.CharField(max_length=50, blank=True, default='')
    model_sha256   = models.CharField(max_length=64, blank=True, default='')
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        # Stamped once, on creation, and never rewritten afterwards — a later
        # save must not quietly re-point an old result at a newer artifact.
        if self._state.adding and self.model_id:
            self.model_version = self.model.version
            self.model_sha256 = self.model.model_file_sha256
        super().save(*args, **kwargs)

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

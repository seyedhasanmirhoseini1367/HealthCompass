import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from .fields import EncryptedCharField


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        PATIENT        = 'patient',        'Patient'
        DOCTOR         = 'doctor',         'Doctor'
        DATA_SCIENTIST = 'data_scientist', 'Data Scientist'
        HOSPITAL_ADMIN = 'hospital_admin', 'Hospital Admin'
        ADMIN          = 'admin',          'Admin'

    # Override AbstractUser.email: add unique + null=True so that users without
    # an email can coexist (NULL != NULL in SQL) while non-null emails are
    # globally unique and safe to use as a login identifier.
    email = models.EmailField(blank=True, null=True, unique=True,
                              verbose_name='email address')
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PATIENT)
    profile_picture = models.ImageField(upload_to='profile_pics/', blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    is_approved = models.BooleanField(default=True,
        help_text='Patients are auto-approved. Doctors, Data Scientists, and Hospital Admins require admin approval.')

    def __str__(self):
        return f'{self.username} ({self.get_role_display()})'

    @property
    def is_patient(self):        return self.role == self.Role.PATIENT
    @property
    def is_doctor(self):         return self.role == self.Role.DOCTOR
    @property
    def is_data_scientist(self): return self.role == self.Role.DATA_SCIENTIST
    @property
    def is_hospital_admin(self): return self.role == self.Role.HOSPITAL_ADMIN


class PatientProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='patient_profile')
    blood_type = models.CharField(max_length=5, blank=True,
        choices=[('A+','A+'),('A-','A-'),('B+','B+'),('B-','B-'),
                 ('AB+','AB+'),('AB-','AB-'),('O+','O+'),('O-','O-')])
    allergies = models.TextField(blank=True)
    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)
    national_id     = EncryptedCharField(
                        blank=True,
                        help_text='Encrypted at rest with a Fernet key derived from SECRET_KEY. '
                                  'Stored as opaque ciphertext in a TEXT column; only the '
                                  'application can read it. Finnish henkilötunnus — '
                                  'treat as highest-sensitivity PII.')
    emergency_token = models.UUIDField(default=uuid.uuid4, unique=True,
                          help_text='Token for public emergency card URL')
    emergency_card_enabled = models.BooleanField(default=True,
                          help_text='Patient can disable to block all public access to their card')

    def regenerate_emergency_token(self):
        self.emergency_token = uuid.uuid4()
        self.save(update_fields=['emergency_token'])

    def __str__(self): return f'Patient: {self.user.username}'


class EmergencyCardView(models.Model):
    """Audit log: every time the public emergency card is successfully accessed."""
    profile   = models.ForeignKey(PatientProfile, on_delete=models.CASCADE,
                    related_name='emergency_views')
    viewed_at = models.DateTimeField(auto_now_add=True)
    ip_hash   = models.CharField(max_length=64,
                    help_text='SHA-256 of visitor IP — never stores raw IP')

    class Meta:
        indexes  = [models.Index(fields=['profile', '-viewed_at'])]
        ordering = ['-viewed_at']

    def __str__(self):
        return f'{self.profile.user.username} card viewed @ {self.viewed_at}'


class DoctorProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='doctor_profile')
    specialty = models.CharField(max_length=100, blank=True)
    license_number = models.CharField(max_length=50, blank=True)
    hospital = models.CharField(max_length=200, blank=True)
    department = models.CharField(max_length=100, blank=True)

    def __str__(self): return f'Dr. {self.user.get_full_name()} - {self.specialty}'


class DataScientistProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='scientist_profile')
    institution = models.CharField(max_length=200, blank=True)
    research_area = models.CharField(max_length=200, blank=True)
    approved_by = models.ForeignKey(CustomUser, null=True, blank=True,
                    on_delete=models.SET_NULL, related_name='approved_scientists')
    approved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self): return f'Scientist: {self.user.username}'


class HospitalAdminProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='hospital_admin_profile')
    hospital_name = models.CharField(max_length=200)
    hospital_code = models.CharField(max_length=50, blank=True)

    def __str__(self): return f'HospAdmin: {self.user.username} @ {self.hospital_name}'


class DoctorAccessLog(models.Model):
    """
    Immutable audit trail: each time a doctor views a patient's records or a
    specific record, one row is appended here.

    In real Kanta-connected systems patients can request a list of who has
    accessed their data. This table is the backing store for that.
    Never delete rows — archive instead.
    """
    actor       = models.ForeignKey(
                    'accounts.CustomUser', on_delete=models.SET_NULL,
                    null=True, related_name='access_log_entries',
                    help_text='The doctor (or admin) who performed the access')
    patient     = models.ForeignKey(
                    'accounts.CustomUser', on_delete=models.SET_NULL,
                    null=True, related_name='access_log_received',
                    help_text='The patient whose data was accessed')
    resource    = models.CharField(
                    max_length=300,
                    help_text='e.g. "patient_records" or "record:<uuid>"')
    accessed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['patient', '-accessed_at'])]
        ordering = ['-accessed_at']

    def __str__(self):
        return f'{self.actor} accessed {self.patient} [{self.resource}] @ {self.accessed_at}'


class ConsentPurpose(models.TextChoices):
    """
    Separate purposes, deliberately not a single "I agree".

    Each names one distinct processing activity a user can accept or decline on
    its own. EXTERNAL_LLM is called out separately from AI_PROCESSING because it
    is the only one that transmits health data to third-party processors
    (Groq, Google, Anthropic, OpenAI) outside this system.
    """
    AI_PROCESSING       = 'ai_processing',       'AI analysis of my health data'
    EXTERNAL_LLM        = 'external_llm',        'Sending my health data to external AI providers'
    DOCUMENT_PROCESSING = 'document_processing', 'Automated reading of my uploaded documents'
    DATA_SHARING        = 'data_sharing',        'Sharing my records with linked clinicians'
    RESEARCH            = 'research',            'Use of anonymised data for research'


class Consent(models.Model):
    """
    One row per consent decision, append-only.

    Granting writes a row. Revoking stamps `revoked_at` and flips `status` on
    that row rather than deleting it; granting again afterwards writes a *new*
    row. The history of who consented to what, when, and under which version is
    therefore never overwritten — which is the point of recording consent at all.

    A partial unique constraint allows at most one GRANTED row per
    (user, purpose); any number of REVOKED rows may accumulate behind it.

    Version: the text the user agreed to is versioned in
    settings.CONSENT_VERSIONS. Consent recorded against an older version no
    longer counts as consent for the current text — see
    apps.accounts.consent.has_consent().
    """
    class Status(models.TextChoices):
        GRANTED = 'granted', 'Granted'
        REVOKED = 'revoked', 'Revoked'

    user       = models.ForeignKey(CustomUser, on_delete=models.CASCADE,
                    related_name='consents')
    purpose    = models.CharField(max_length=32, choices=ConsentPurpose.choices)
    version    = models.CharField(max_length=20,
                    help_text='Version of the consent text the user agreed to, e.g. "v1".')
    status     = models.CharField(max_length=10, choices=Status.choices, default=Status.GRANTED)
    granted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'purpose'],
                condition=models.Q(status='granted'),
                name='unique_active_consent_per_user_purpose',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'purpose', 'status']),
        ]

    def __str__(self):
        # Deliberately identifies the decision, not its content — this string
        # can reach admin listings.
        return f'{self.user_id}: {self.purpose} [{self.status}@{self.version}]'

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.GRANTED and self.revoked_at is None


class PatientDoctorRelationship(models.Model):
    patient = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='my_doctors')
    doctor = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='my_patients')
    linked_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL,
                    null=True, blank=True, related_name='relationships_created')
    hospital = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ['patient', 'doctor']

    def __str__(self): return f'{self.patient.username} <-> Dr. {self.doctor.username}'

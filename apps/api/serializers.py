from rest_framework import serializers

from apps.accounts.models import CustomUser, SharingGrant
from apps.medical_records.models import MedicalRecord
from apps.ai_insights.models import AIModel, ModelPrediction, HealthAlert
from apps.notifications.models import Notification
from apps.appointments.models import Appointment
from apps.care.models import CareTask, MonitoringSignal, PatientReport, TaskOccurrence


class UserSerializer(serializers.ModelSerializer):
    full_name       = serializers.SerializerMethodField()
    role_display    = serializers.CharField(source='get_role_display', read_only=True)
    profile_picture = serializers.SerializerMethodField()

    def get_full_name(self, obj) -> str:
        return obj.get_full_name() or obj.username

    def get_profile_picture(self, obj) -> str | None:
        if not obj.profile_picture:
            return None
        url = obj.profile_picture.url
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(url)
        # Fallback: absolute URL from the configured site domain.
        if not url.startswith('http'):
            from django.conf import settings
            return f'{settings.ACCOUNT_DEFAULT_HTTP_PROTOCOL}://{settings.SITE_DOMAIN}{url}'
        return url

    class Meta:
        model  = CustomUser
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'full_name',
                  'role', 'role_display', 'is_approved', 'profile_picture',
                  'phone_number', 'date_of_birth']
        read_only_fields = ['id', 'username', 'email', 'role', 'role_display',
                            'is_approved', 'profile_picture', 'full_name']


class RegisterSerializer(serializers.ModelSerializer):
    """
    Self-service registration always creates a PATIENT.

    `role` is deliberately NOT a writable field. It was previously accepted from
    the request body and passed straight to create_user() while `is_approved`
    defaults to True, so anyone could self-register as `doctor`,
    `data_scientist`, `hospital_admin` or `admin` and inherit that role's
    privileges. The server-rendered signup form never exposed `role`, so this
    only ever affected the API.

    Professional roles are assigned by an administrator (Django admin), which is
    also where `is_approved` is granted.
    """
    password   = serializers.CharField(write_only=True, min_length=8)
    password2  = serializers.CharField(write_only=True)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name  = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model  = CustomUser
        fields = ['email', 'password', 'password2', 'first_name', 'last_name']
        # Uniqueness is decided in validate_email / auth_service, not by the
        # model's unique validator, so a taken address can be answered neutrally.
        extra_kwargs = {'email': {'validators': []}}

    def validate(self, data):
        if data['password'] != data['password2']:
            raise serializers.ValidationError({'password': 'Passwords do not match.'})
        from apps.accounts.auth_service import password_errors
        errors = password_errors(data['password'], CustomUser(
            email=data.get('email', ''), first_name=data.get('first_name', ''),
            last_name=data.get('last_name', '')))
        if errors:
            raise serializers.ValidationError({'password': errors})
        return data

    def validate_email(self, value):
        from apps.accounts.auth_service import verification_required
        if not verification_required() and CustomUser.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def create(self, validated_data):
        # One registration path for web and API (apps.accounts.auth_service):
        # validators, a generated username without '@', profile, EmailAddress
        # row and the confirmation mail.
        from apps.accounts import auth_service
        try:
            return auth_service.register_patient(
                validated_data['email'], validated_data['password'],
                validated_data.get('first_name', ''), validated_data.get('last_name', ''),
                request=self.context.get('request'))
        except auth_service.AuthError as exc:
            raise serializers.ValidationError({'email': exc.message}) from exc


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """Fields a user may change on their own profile through the API."""

    class Meta:
        model  = CustomUser
        fields = ['first_name', 'last_name', 'phone_number', 'date_of_birth']
        extra_kwargs = {f: {'required': False} for f in fields}

    def validate_phone_number(self, value):
        from apps.accounts.validators import PHONE_MESSAGE, PHONE_RE
        if value and not PHONE_RE.match(value):
            raise serializers.ValidationError(PHONE_MESSAGE)
        return value


class MedicalRecordSerializer(serializers.ModelSerializer):
    record_type_display = serializers.CharField(source='get_record_type_display', read_only=True)

    class Meta:
        model  = MedicalRecord
        fields = [
            'id', 'title', 'record_type', 'record_type_display',
            'record_date', 'uploaded_at', 'is_flagged', 'notes',
            'parsed_data',
        ]
        read_only_fields = ['uploaded_at', 'is_flagged']


class MedicalRecordListSerializer(serializers.ModelSerializer):
    """List rows: no parsed_data or raw_text (those are on the detail endpoint)."""
    record_type_display = serializers.CharField(source='get_record_type_display', read_only=True)

    class Meta:
        model  = MedicalRecord
        fields = ['id', 'title', 'record_type', 'record_type_display',
                  'record_date', 'uploaded_at', 'is_flagged', 'notes']


class ParsedLabValueSerializer(serializers.ModelSerializer):
    """The reading that currently stands: a correction, when one exists, replaces
    the extracted value, unit and flags, as on the web shared-record view."""
    is_corrected = serializers.SerializerMethodField()

    class Meta:
        from apps.medical_records.models import ParsedLabValue
        model  = ParsedLabValue
        fields = ['parameter_name', 'value', 'unit', 'reference_range',
                  'is_abnormal', 'is_critical', 'measured_at', 'is_corrected']

    def to_representation(self, obj):
        data = super().to_representation(obj)
        current = obj.effective()
        if current is not obj:
            for field in ('value', 'unit', 'is_abnormal', 'is_critical'):
                data[field] = getattr(current, field)
        return data

    def get_is_corrected(self, obj) -> bool:
        return obj.effective() is not obj


class WearableDataPointSerializer(serializers.ModelSerializer):
    metric_display = serializers.CharField(source='get_metric_display', read_only=True)

    class Meta:
        from apps.medical_records.models import WearableDataPoint
        model  = WearableDataPoint
        fields = ['metric', 'metric_display', 'value', 'unit', 'recorded_at']


class MedicalRecordDetailSerializer(serializers.ModelSerializer):
    record_type_display = serializers.CharField(source='get_record_type_display', read_only=True)
    lab_values          = ParsedLabValueSerializer(many=True, read_only=True)
    wearable_points     = WearableDataPointSerializer(many=True, read_only=True)

    class Meta:
        model  = MedicalRecord
        fields = [
            'id', 'title', 'record_type', 'record_type_display',
            'record_date', 'uploaded_at', 'is_flagged', 'notes',
            'parsed_data', 'raw_text', 'lab_values', 'wearable_points',
        ]
        read_only_fields = ['uploaded_at', 'is_flagged']


class HealthAlertSerializer(serializers.ModelSerializer):
    severity_display = serializers.CharField(source='get_severity_display', read_only=True)

    class Meta:
        model  = HealthAlert
        fields = ['id', 'severity', 'severity_display', 'title', 'message', 'is_read', 'created_at']


class ModelPredictionSerializer(serializers.ModelSerializer):
    model_name     = serializers.CharField(source='model.name', read_only=True)
    model_category = serializers.CharField(source='model.category', read_only=True)
    model_slug     = serializers.CharField(source='model.slug', read_only=True)
    risk_pct       = serializers.SerializerMethodField()
    result_label   = serializers.SerializerMethodField()

    def get_risk_pct(self, obj) -> float | None:
        return round(float(obj.risk_score) * 100, 1) if obj.risk_score is not None else None

    def get_result_label(self, obj) -> str:
        return obj.result.get('label') or obj.result.get('prediction_label', '') if obj.result else ''

    class Meta:
        model  = ModelPrediction
        fields = ['id', 'model_name', 'model_category', 'model_slug', 'risk_score', 'risk_pct',
                  'result_label', 'interpretation', 'result', 'input_data', 'created_at']


class AIModelListSerializer(serializers.ModelSerializer):
    category_display   = serializers.CharField(source='get_category_display', read_only=True)
    input_type_display = serializers.CharField(source='get_input_type_display', read_only=True)

    class Meta:
        model  = AIModel
        fields = ['id', 'name', 'slug', 'description', 'category', 'category_display',
                  'input_type', 'input_type_display', 'run_count']


class NotificationSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source='get_type_display', read_only=True)

    class Meta:
        model  = Notification
        fields = ['id', 'type', 'type_display', 'title', 'message', 'is_read', 'link', 'created_at']


class AppointmentSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Appointment
        fields = [
            'id', 'title', 'doctor_name', 'location', 'appointment_datetime',
            'notes', 'remind_24h', 'remind_3h', 'remind_2h', 'remind_1h',
            'is_completed', 'is_cancelled', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class SharingGrantSerializer(serializers.ModelSerializer):
    """
    Read-only: writes always go through the dedicated create/revoke actions
    (apps.api.views.sharing), never through this serializer's .save(), so the
    business rules in accounts.authz cannot be bypassed by posting a grant
    directly.
    """
    patient      = UserSerializer(read_only=True)
    recipient    = UserSerializer(read_only=True)
    is_effective = serializers.BooleanField(read_only=True)

    class Meta:
        model  = SharingGrant
        fields = ['id', 'patient', 'recipient', 'can_view_records', 'can_view_alerts',
                  'can_view_appointments', 'status', 'is_effective', 'created_at',
                  'expires_at', 'data_cutoff', 'revoked_at', 'revoke_reason']
        read_only_fields = fields


class CareTaskSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source='get_kind_display', read_only=True)

    class Meta:
        model  = CareTask
        fields = ['id', 'kind', 'kind_display', 'label', 'times_of_day',
                  'grace_minutes', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'is_active', 'created_at', 'updated_at']


class TaskOccurrenceSerializer(serializers.ModelSerializer):
    task_label = serializers.CharField(source='task.label', read_only=True)
    task_kind  = serializers.CharField(source='task.kind', read_only=True)

    class Meta:
        model  = TaskOccurrence
        fields = ['id', 'task', 'task_label', 'task_kind', 'due_at', 'state',
                  'responded_at', 'response_input', 'created_at']
        read_only_fields = fields


class PatientReportSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PatientReport
        fields = ['id', 'kind', 'input_method', 'text', 'occurred_at', 'created_at']
        read_only_fields = ['id', 'input_method', 'created_at']


class MonitoringSignalSerializer(serializers.ModelSerializer):
    """
    headline/detail mirror accounts.views._present_signal exactly (same four
    branches, same wording) so a caregiver reads the same sentence on mobile
    and web. Kept as a local copy rather than importing that view-private
    function into the API layer.
    """
    kind_display     = serializers.CharField(source='get_kind_display', read_only=True)
    severity_display = serializers.CharField(source='get_severity_display', read_only=True)
    headline         = serializers.SerializerMethodField()
    detail           = serializers.SerializerMethodField()

    def get_headline(self, obj):
        return _present_signal(obj)['headline']

    def get_detail(self, obj):
        return _present_signal(obj)['detail']

    class Meta:
        model  = MonitoringSignal
        fields = ['id', 'kind', 'kind_display', 'severity', 'severity_display',
                  'window_start', 'window_end', 'created_at', 'resolved_at',
                  'headline', 'detail']
        read_only_fields = fields


def _present_signal(signal):
    """
    What a caregiver reads about one care signal.

    Mirrors accounts.views._present_signal exactly: says what was observed
    and stops there. "Not confirmed 3 times" is a fact about this
    application's records; "not taking her medication" would be a claim
    about a person that the evidence — silence — cannot support.
    """
    if signal.kind == MonitoringSignal.Kind.REPEATED_UNCONFIRMED:
        count = signal.occurrences.count()
        first = signal.occurrences.first()
        label = first.task.label if first else 'a care task'
        return {
            'headline': f'{count} unanswered reminder{"" if count == 1 else "s"}',
            'detail':   f'“{label}” was not confirmed {count} time'
                        f'{"" if count == 1 else "s"} in a row. That means no one '
                        f'answered — not that it was missed.',
        }
    if signal.kind == MonitoringSignal.Kind.REPORTED_MISSED:
        return {'headline': 'Reported a missed task',
                'detail': 'They told the app they missed a scheduled task.'}
    if signal.kind == MonitoringSignal.Kind.REPORTED_SYMPTOM:
        entry = signal.reports.first()
        return {'headline': 'Reported how they are feeling',
                'detail': entry.text if entry else ''}
    return {'headline': 'Needs attention', 'detail': ''}

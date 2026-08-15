from django.contrib import admin

from apps.accounts.admin_phi import PhiAccessLoggedAdmin, NonEditablePhiAdmin
from .models import (LabValueCorrection, MedicalRecord, ParsedLabValue,
                     WearableDataPoint)

class LabValueInline(admin.TabularInline):
    model = ParsedLabValue
    extra = 0

@admin.register(MedicalRecord)
class MedicalRecordAdmin(NonEditablePhiAdmin, admin.ModelAdmin):
    list_display  = ('title', 'patient', 'record_type', 'source', 'record_date', 'is_flagged')
    list_filter   = ('record_type', 'source', 'is_flagged')
    search_fields = ('title', 'patient__username')
    inlines       = [LabValueInline]

@admin.register(ParsedLabValue)
class ParsedLabValueAdmin(PhiAccessLoggedAdmin, admin.ModelAdmin):
    """
    Extracted values are read-only here. Corrections are appended instead.

    Editing this row in place used to be the only way to fix a misread value,
    and it destroyed what the source document actually said — including the
    evidence for any alert that had already fired on the original number.

    The fields stay visible because reading them is how someone confirms a
    correction is needed; they are simply no longer writable.
    """
    list_display  = ('parameter_name', 'value', 'unit', 'effective_display',
                     'patient', 'is_critical')
    list_filter   = ('unit_known', 'is_abnormal', 'is_critical')
    search_fields = ('parameter_name', 'record__patient__username')
    readonly_fields = ('record', 'patient', 'parameter_name', 'value', 'unit',
                       'canonical_value', 'original_unit', 'unit_known',
                       'reference_range', 'is_abnormal', 'is_critical', 'measured_at')

    def phi_subject(self, obj):
        return obj.patient or getattr(obj.record, 'patient', None)

    @admin.display(description='Effective value')
    def effective_display(self, obj):
        current = obj.effective()
        if current is obj:
            return '—'
        return f'{current.value} {current.unit} (corrected)'

    def has_add_permission(self, request):
        # Values are created by ingestion, never by hand: a value typed here
        # would have no source document behind it.
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Deleting an extracted value destroys the evidence a correction exists
        # to preserve. Delete the record if the whole document was wrong.
        return False


@admin.register(LabValueCorrection)
class LabValueCorrectionAdmin(PhiAccessLoggedAdmin, admin.ModelAdmin):
    """
    Append a corrected reading. The original is never touched.

    Authority comes from `authz.can_correct_clinical_value`, not from a local
    role check — the same seam every other privileged read and write in this
    system asks.
    """
    list_display  = ('created_at', 'original', 'value', 'unit', 'actor_label')
    search_fields = ('original__parameter_name', 'reason', 'actor_label')
    readonly_fields = ('actor', 'actor_label', 'created_at')

    def phi_subject(self, obj):
        return getattr(obj.original, 'patient', None)

    def _may_correct(self, request):
        from apps.accounts.authz import can_correct_clinical_value
        return can_correct_clinical_value(request.user)

    def has_add_permission(self, request):
        return self._may_correct(request)

    def has_change_permission(self, request, obj=None):
        # A correction is evidence in the same way the original is. Wrong
        # correction? Append another one.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        from django.core.exceptions import PermissionDenied

        from apps.accounts.audit import record as record_admin_action
        from apps.accounts.models import AdminAuditEvent

        if not self._may_correct(request):
            record_admin_action(
                AdminAuditEvent.Action.VALUE_CORRECTED, actor=request.user,
                target=obj.original, success=False, reason='not_authorised')
            raise PermissionDenied('You are not authorised to correct clinical values.')

        obj.actor = request.user
        super().save_model(request, obj, form, change)

        # Identifiers only. The analyte name and both values are clinical
        # content and stay out of a table compliance staff can read; the
        # correction row itself holds them, under the patient's own record.
        record_admin_action(
            AdminAuditEvent.Action.VALUE_CORRECTED, actor=request.user,
            target=obj.original,
            lab_value_id=str(obj.original_id), correction_id=str(obj.pk))

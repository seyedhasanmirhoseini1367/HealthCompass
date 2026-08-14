from django.contrib import admin

from apps.accounts.admin_phi import PhiAccessLoggedAdmin
from .models import MedicalRecord, ParsedLabValue, WearableDataPoint

class LabValueInline(admin.TabularInline):
    model = ParsedLabValue
    extra = 0

@admin.register(MedicalRecord)
class MedicalRecordAdmin(PhiAccessLoggedAdmin, admin.ModelAdmin):
    list_display  = ('title', 'patient', 'record_type', 'source', 'record_date', 'is_flagged')
    list_filter   = ('record_type', 'source', 'is_flagged')
    search_fields = ('title', 'patient__username')
    inlines       = [LabValueInline]

admin.site.register(ParsedLabValue)

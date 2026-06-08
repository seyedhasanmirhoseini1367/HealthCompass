from django.contrib import admin
from .models import CareCircle, CareGiver, DailyCheckIn, MedicationSchedule, MedicationLog


class CareGiverInline(admin.TabularInline):
    model = CareGiver
    extra = 0
    readonly_fields = ('access_token', 'invited_at', 'last_viewed')


@admin.register(CareCircle)
class CareCircleAdmin(admin.ModelAdmin):
    list_display = ('patient', 'is_active', 'created_at')
    inlines = [CareGiverInline]


@admin.register(DailyCheckIn)
class DailyCheckInAdmin(admin.ModelAdmin):
    list_display  = ('patient', 'date', 'feeling_score', 'pain_score', 'mood', 'is_flagged')
    list_filter   = ('is_flagged', 'mood')
    search_fields = ('patient__username', 'symptoms')


@admin.register(MedicationSchedule)
class MedicationScheduleAdmin(admin.ModelAdmin):
    list_display = ('name', 'patient', 'dosage', 'time_of_day', 'is_active')
    list_filter  = ('time_of_day', 'is_active')

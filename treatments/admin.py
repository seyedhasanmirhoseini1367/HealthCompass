from django.contrib import admin
from .models import TreatmentCourse, TreatmentMilestone, CourseMonitor


class MilestoneInline(admin.TabularInline):
    model = TreatmentMilestone
    extra = 0


class MonitorInline(admin.TabularInline):
    model = CourseMonitor
    extra = 0


@admin.register(TreatmentCourse)
class TreatmentCourseAdmin(admin.ModelAdmin):
    list_display  = ('name', 'patient', 'condition', 'specialty', 'status', 'start_date')
    list_filter   = ('status', 'specialty')
    search_fields = ('name', 'condition', 'patient__username')
    inlines       = [MilestoneInline, MonitorInline]

from django.contrib import admin
from .models import Appointment


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ['title', 'patient', 'appointment_datetime', 'is_completed', 'is_cancelled']
    list_filter  = ['is_completed', 'is_cancelled', 'remind_24h', 'remind_1h']
    search_fields = ['title', 'doctor_name', 'patient__email']
    date_hierarchy = 'appointment_datetime'

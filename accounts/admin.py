from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (CustomUser, PatientProfile, DoctorProfile,
                     DataScientistProfile, HospitalAdminProfile,
                     PatientDoctorRelationship)

admin.site.site_header = 'HealthCompass Admin'
admin.site.site_title  = 'HealthCompass'
admin.site.index_title = 'Platform Administration'


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display   = ('username', 'email', 'get_full_name', 'role', 'is_approved', 'is_active', 'date_joined')
    list_filter    = ('role', 'is_approved', 'is_active')
    list_editable  = ('is_approved',)
    search_fields  = ('username', 'email', 'first_name', 'last_name')
    actions        = ['approve_users']
    fieldsets      = UserAdmin.fieldsets + (
        ('HealthCompass', {'fields': ('role', 'profile_picture', 'phone_number',
                                      'date_of_birth', 'is_approved')}),
    )

    @admin.action(description='✅ Approve selected users')
    def approve_users(self, request, queryset):
        count = queryset.update(is_approved=True)
        self.message_user(request, f'{count} user(s) approved.')


@admin.register(PatientProfile)
class PatientProfileAdmin(admin.ModelAdmin):
    list_display  = ('user', 'blood_type', 'emergency_contact_name')
    search_fields = ('user__username',)


@admin.register(DoctorProfile)
class DoctorProfileAdmin(admin.ModelAdmin):
    list_display  = ('user', 'specialty', 'hospital', 'license_number')
    search_fields = ('user__username', 'specialty', 'hospital')


@admin.register(DataScientistProfile)
class DataScientistProfileAdmin(admin.ModelAdmin):
    list_display  = ('user', 'institution', 'research_area', 'approved_by')
    search_fields = ('user__username', 'institution')


@admin.register(HospitalAdminProfile)
class HospitalAdminProfileAdmin(admin.ModelAdmin):
    list_display  = ('user', 'hospital_name', 'hospital_code')
    search_fields = ('hospital_name', 'user__username')


@admin.register(PatientDoctorRelationship)
class PatientDoctorRelationshipAdmin(admin.ModelAdmin):
    list_display  = ('patient', 'doctor', 'linked_by', 'is_active', 'created_at')
    list_filter   = ('is_active',)
    search_fields = ('patient__username', 'doctor__username')
    list_editable = ('is_active',)

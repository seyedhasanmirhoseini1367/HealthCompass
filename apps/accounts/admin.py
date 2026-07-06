from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.core.mail import send_mail
from django.conf import settings
from .models import (CustomUser, PatientProfile, DoctorProfile,
                     DataScientistProfile, HospitalAdminProfile,
                     PatientDoctorRelationship)

admin.site.site_header = 'HealthCompass Admin'
admin.site.site_title  = 'HealthCompass'
admin.site.index_title = 'Platform Administration'


def _send_approval_email(user, approved):
    if not user.email:
        return
    if approved:
        subject = '[HealthCompass] Your account has been approved'
        body = (
            f'Hi {user.get_full_name() or user.username},\n\n'
            f'Your HealthCompass account ({user.get_role_display()}) has been approved.\n'
            f'You can now log in at https://{settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else "healthcompass.hasanai.net"}/accounts/login/\n\n'
            f'Welcome aboard!\nThe HealthCompass Team'
        )
    else:
        subject = '[HealthCompass] Your account registration was not approved'
        body = (
            f'Hi {user.get_full_name() or user.username},\n\n'
            f'Unfortunately your HealthCompass account registration as {user.get_role_display()} '
            f'could not be approved at this time.\n\n'
            f'If you believe this is an error, please contact us.\nThe HealthCompass Team'
        )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display   = ('username', 'email', 'get_full_name', 'role', 'is_approved', 'is_active', 'date_joined')
    list_filter    = ('role', 'is_approved', 'is_active')
    list_editable  = ('is_approved',)
    search_fields  = ('username', 'email', 'first_name', 'last_name')
    actions        = ['approve_users', 'reject_users']
    fieldsets      = UserAdmin.fieldsets + (
        ('HealthCompass', {'fields': ('role', 'profile_picture', 'phone_number',
                                      'date_of_birth', 'is_approved')}),
    )

    @admin.action(description='✅ Approve selected users')
    def approve_users(self, request, queryset):
        updated = queryset.filter(is_approved=False)
        for user in updated:
            _send_approval_email(user, approved=True)
        count = queryset.update(is_approved=True)
        self.message_user(request, f'{count} user(s) approved and notified by email.')

    @admin.action(description='❌ Reject & delete selected users')
    def reject_users(self, request, queryset):
        count = 0
        for user in queryset:
            _send_approval_email(user, approved=False)
            user.delete()
            count += 1
        self.message_user(request, f'{count} user(s) rejected, notified, and removed.')


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

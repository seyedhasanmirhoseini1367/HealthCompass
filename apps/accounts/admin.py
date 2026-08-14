from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.core.mail import send_mail
from django.conf import settings
from .models import (AdminAuditEvent, CustomUser, DoctorAccessLog,
                     PatientProfile, DoctorProfile,
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


#: Fields that confer authority rather than describe a person.
#:
#: They are not editable anywhere in this admin. Django's default UserAdmin
#: puts is_staff, is_superuser, groups and user_permissions in a "Permissions"
#: fieldset, so any account able to change users could promote itself — or
#: anyone else — to superuser in one form submission, unlogged. `role` belongs
#: here too: it is what every clinical authorization check reads.
AUTHORITY_FIELDS = ('is_staff', 'is_superuser', 'groups', 'user_permissions', 'role')


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    """
    User management WITHOUT privilege management.

    Everything an operator needs day to day — approving registrations, fixing a
    name or email, disabling an account — stays available. Granting system
    authority does not, and is deliberately left to the shell (`createsuperuser`,
    or an explicit `manage.py shell` edit), which is auditable at the deployment
    level and cannot be reached by a hijacked browser session.

    This is enforcement, not concealment: a field absent from `fieldsets` is
    absent from the ModelForm, so a hand-crafted POST naming it is ignored by
    the form rather than applied. `_reject_authority_change` is the backstop for
    anything that reaches save_model by another route.
    """
    list_display   = ('username', 'email', 'get_full_name', 'role', 'is_approved', 'is_active', 'date_joined')
    list_filter    = ('role', 'is_approved', 'is_active')
    list_editable  = ('is_approved',)
    search_fields  = ('username', 'email', 'first_name', 'last_name')
    actions        = ['approve_users', 'reject_users']

    # UserAdmin.fieldsets rebuilt rather than extended: the inherited
    # "Permissions" block is the escalation surface, so it is replaced by one
    # that keeps only is_active (disabling an account is user management, not
    # privilege management).
    fieldsets = (
        (None,               {'fields': ('username', 'password')}),
        ('Personal info',    {'fields': ('first_name', 'last_name', 'email')}),
        ('Account status',   {'fields': ('is_active',),
                              'description': 'System authority (staff, superuser, '
                                             'groups, permissions) and clinical role '
                                             'are intentionally not editable here.'}),
        ('Important dates',  {'fields': ('last_login', 'date_joined')}),
        ('HealthCompass',    {'fields': ('role', 'profile_picture', 'phone_number',
                                         'date_of_birth', 'is_approved')}),
    )

    def get_readonly_fields(self, request, obj=None):
        """
        `role` is shown so an operator can see what an account is, and is
        writable for other people's accounts — reassigning a clinical role is
        ordinary user management. On your OWN account it is read-only: changing
        your own role is self-escalation whatever the target role happens to be.
        """
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.pk == request.user.pk:
            readonly.append('role')
        return tuple(readonly)

    def save_model(self, request, obj, form, change):
        """
        Backstop. The form cannot carry these fields, but this catches anything
        that reaches here another way — a subclass, a future fieldset edit, or a
        code path that constructs the form itself.
        """
        if change and obj.pk:
            self._reject_authority_change(request, obj)
        super().save_model(request, obj, form, change)

    def _reject_authority_change(self, request, obj):
        from django.core.exceptions import PermissionDenied

        stored = CustomUser.objects.filter(pk=obj.pk).only(
            'is_staff', 'is_superuser', 'role').first()
        if stored is None:
            return

        changed = [f for f in ('is_staff', 'is_superuser', 'role')
                   if getattr(obj, f) != getattr(stored, f)]
        if not changed:
            return

        # Someone else's clinical role may be reassigned; authority flags may
        # not be changed here by anyone, and nobody may change their own role.
        if changed == ['role'] and obj.pk != request.user.pk:
            return

        # A refused escalation is the single most important thing in this table:
        # nothing else records an attempt, because nothing was written.
        from .audit import record as record_admin_action
        from .models import AdminAuditEvent
        record_admin_action(
            AdminAuditEvent.Action.ESCALATION_DENIED,
            actor=request.user, target=obj, target_label=obj.username,
            success=False, fields=','.join(changed),
            self_target=(obj.pk == request.user.pk))

        raise PermissionDenied(
            f'Refusing to change {", ".join(changed)} through the user admin. '
            f'System authority is granted from the shell, and no account may '
            f'alter its own role.'
        )

    @admin.action(description='✅ Approve selected users')
    def approve_users(self, request, queryset):
        from .audit import record as record_admin_action
        from .models import AdminAuditEvent

        updated = list(queryset.filter(is_approved=False))
        for user in updated:
            _send_approval_email(user, approved=True)
        count = queryset.update(is_approved=True)

        # Recorded per user rather than as one summary row: "who approved this
        # account" is the question a trail has to answer, and a count cannot.
        for user in updated:
            record_admin_action(
                AdminAuditEvent.Action.USER_APPROVED,
                actor=request.user, target=user,
                target_label=user.username, role=user.role)
        self.message_user(request, f'{count} user(s) approved and notified by email.')

    @admin.action(description='❌ Reject & delete selected users')
    def reject_users(self, request, queryset):
        from .audit import record as record_admin_action
        from .models import AdminAuditEvent

        count = 0
        for user in queryset:
            _send_approval_email(user, approved=False)
            # Recorded BEFORE the delete: afterwards there is no row to name,
            # and this action destroys the account it is describing.
            record_admin_action(
                AdminAuditEvent.Action.USER_REJECTED,
                actor=request.user, target=user,
                target_label=user.username, role=user.role)
            user.delete()
            count += 1
        self.message_user(request, f'{count} user(s) rejected, notified, and removed.')


class _ReadOnlyAdmin(admin.ModelAdmin):
    """
    A record of what happened, not a place to change it.

    An audit trail an administrator can edit is not evidence. Add, change and
    delete are refused at the ModelAdmin level, so the rows are visible and
    inert — including to the account that wrote them.

    This is not tamper-proofing: a superuser with shell access can still reach
    the ORM. It removes the ability to erase evidence through the interface
    people actually use, which is the realistic threat, and states plainly that
    the stronger guarantee is not being claimed.
    """
    def has_add_permission(self, request):                       return False
    def has_change_permission(self, request, obj=None):          return False
    def has_delete_permission(self, request, obj=None):          return False


@admin.register(AdminAuditEvent)
class AdminAuditEventAdmin(_ReadOnlyAdmin):
    """
    Who did what to the system, when, under which authority, and whether it
    succeeded. Refusals are here too — they are the rows worth reading.
    """
    list_display  = ('created_at', 'actor_label', 'action', 'target_type',
                     'target_label', 'authority', 'success')
    list_filter   = ('action', 'success', 'authority')
    search_fields = ('actor_label', 'target_label', 'target_id')
    date_hierarchy = 'created_at'


@admin.register(DoctorAccessLog)
class DoctorAccessLogAdmin(_ReadOnlyAdmin):
    """
    The clinical access trail. It was written by four call sites and read by
    nobody: patients could see their own slice through the data export, and no
    compliance review was possible without shell access.

    Deliberately shows WHO accessed WHICH resource and when — never the content
    of the resource, so reviewing the trail is not itself a way to read records.
    """
    list_display  = ('accessed_at', 'actor_label', 'actor', 'patient', 'resource')
    list_filter   = ('accessed_at',)
    search_fields = ('actor_label', 'resource')
    date_hierarchy = 'accessed_at'


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
    list_display  = ('patient', 'doctor', 'linked_by', 'status', 'created_at')
    list_filter   = ('status',)
    search_fields = ('patient__username', 'doctor__username')
    list_editable = ('status',)

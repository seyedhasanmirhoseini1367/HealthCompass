"""
REGRESSION — F1: the user admin was a self-escalation path.

`CustomUserAdmin` extended Django's `UserAdmin` fieldsets, which include a
"Permissions" block of `is_staff`, `is_superuser`, `groups` and
`user_permissions`. Any account able to change users could therefore promote
itself — or any other account — to superuser in one form submission, with no
audit record anywhere. `role`, which every clinical authorization check reads,
was editable on one's own account for the same reason.

Verified before the fix, from the effective admin configuration:

    Permissions ['is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions']
    readonly_fields ()

The fix removes those fields from the form rather than hiding them, so a
hand-crafted POST naming them is ignored by the ModelForm instead of applied.
`save_model` carries a backstop for anything reaching it another way.

These tests drive the real admin over HTTP. A test that only inspected
`fieldsets` would pass against an implementation that still accepted the POST.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.admin import AUTHORITY_FIELDS

User = get_user_model()

CHANGE = 'admin:accounts_customuser_change'


class _AdminClient(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(
            'esc_admin', email='esc_admin@test.invalid', password='pw-admin-1')
        self.victim = User.objects.create_user(
            'esc_user', email='esc_user@test.invalid', password='pw-user-1',
            role='patient')
        self.client.force_login(self.admin)

    def _post(self, target, **overrides):
        """Submit the change form with a full, valid payload plus overrides."""
        payload = {
            'username': target.username,
            'email': target.email or '',
            'first_name': target.first_name,
            'last_name': target.last_name,
            'is_active': 'on' if target.is_active else '',
            'role': target.role,
            'phone_number': target.phone_number,
            'is_approved': 'on' if target.is_approved else '',
            'last_login_0': '', 'last_login_1': '',
            'date_joined_0': '2026-01-01', 'date_joined_1': '00:00:00',
            '_continue': 'Save and continue editing',
        }
        payload.update(overrides)
        return self.client.post(reverse(CHANGE, args=[target.pk]), payload)


class AuthorityFieldsAreNotInTheFormTests(_AdminClient):

    def test_the_form_does_not_offer_authority_fields(self):
        """ACCEPTANCE — F1. These were in the inherited Permissions fieldset."""
        response = self.client.get(reverse(CHANGE, args=[self.victim.pk]))
        self.assertEqual(response.status_code, 200)

        form_fields = set(response.context['adminform'].form.fields)
        for field in ('is_staff', 'is_superuser', 'groups', 'user_permissions'):
            with self.subTest(field=field):
                self.assertNotIn(field, form_fields)

    def test_the_declared_authority_set_is_complete(self):
        self.assertEqual(
            set(AUTHORITY_FIELDS),
            {'is_staff', 'is_superuser', 'groups', 'user_permissions', 'role'})

    def test_no_fieldset_mentions_them(self):
        from django.contrib import admin as dj

        from apps.accounts.admin import CustomUserAdmin
        ma = CustomUserAdmin(User, dj.site)
        declared = [f for _, opts in ma.fieldsets for f in opts.get('fields', ())]
        for field in ('is_staff', 'is_superuser', 'groups', 'user_permissions'):
            self.assertNotIn(field, declared)


class SelfEscalationIsRefusedTests(_AdminClient):
    """The POST must not work, not merely be absent from the rendered page."""

    def test_posting_is_superuser_on_another_account_does_nothing(self):
        """ACCEPTANCE — F1. This granted superuser."""
        self._post(self.victim, is_superuser='on', is_staff='on')

        self.victim.refresh_from_db()
        self.assertFalse(self.victim.is_superuser)
        self.assertFalse(self.victim.is_staff)

    def test_posting_is_superuser_on_your_own_account_does_nothing(self):
        staff = User.objects.create_user(
            'esc_staff', email='esc_staff@test.invalid', password='pw-staff-1',
            is_staff=True)
        staff.user_permissions.add(
            *__import__('django.contrib.auth.models', fromlist=['Permission'])
            .Permission.objects.filter(codename__in=('change_customuser', 'view_customuser')))
        self.client.force_login(staff)

        self._post(staff, is_superuser='on')

        staff.refresh_from_db()
        self.assertFalse(staff.is_superuser)

    def test_changing_your_own_role_is_refused(self):
        response = self._post(self.admin, role='doctor')
        self.assertIn(response.status_code, (200, 302, 403))

        self.admin.refresh_from_db()
        self.assertNotEqual(self.admin.role, 'doctor')

    def test_your_own_role_is_read_only_in_the_form(self):
        response = self.client.get(reverse(CHANGE, args=[self.admin.pk]))
        self.assertIn('role', response.context['adminform'].readonly_fields)

    def test_someone_elses_role_is_editable(self):
        response = self.client.get(reverse(CHANGE, args=[self.victim.pk]))
        self.assertNotIn('role', response.context['adminform'].readonly_fields)

    def test_the_save_model_backstop_refuses_authority_changes(self):
        """
        Reaching save_model with a mutated object — a subclass, a future
        fieldset edit, or code that builds the form itself.
        """
        from django.core.exceptions import PermissionDenied

        from django.contrib import admin as dj

        from apps.accounts.admin import CustomUserAdmin
        ma = CustomUserAdmin(User, dj.site)

        request = type('R', (), {'user': self.admin})()
        self.victim.is_superuser = True

        with self.assertRaises(PermissionDenied):
            ma.save_model(request, self.victim, form=None, change=True)

        self.victim.refresh_from_db()
        self.assertFalse(self.victim.is_superuser)


class OrdinaryUserManagementStillWorksTests(_AdminClient):
    """The fix must not cost the operator their actual daily work."""

    def test_the_changelist_loads(self):
        response = self.client.get(reverse('admin:accounts_customuser_changelist'))
        self.assertEqual(response.status_code, 200)

    def test_personal_details_can_be_corrected(self):
        self._post(self.victim, first_name='Corrected', email='fixed@test.invalid')

        self.victim.refresh_from_db()
        self.assertEqual(self.victim.first_name, 'Corrected')
        self.assertEqual(self.victim.email, 'fixed@test.invalid')

    def test_an_account_can_be_disabled_and_re_enabled(self):
        self._post(self.victim, is_active='')
        self.victim.refresh_from_db()
        self.assertFalse(self.victim.is_active)

        self._post(self.victim, is_active='on')
        self.victim.refresh_from_db()
        self.assertTrue(self.victim.is_active)

    def test_another_users_clinical_role_can_be_reassigned(self):
        self._post(self.victim, role='doctor')
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.role, 'doctor')

    def test_the_approval_action_still_works(self):
        pending = User.objects.create_user(
            'esc_pending', email='esc_pending@test.invalid', password='pw-p-1',
            role='doctor', is_approved=False)

        self.client.post(reverse('admin:accounts_customuser_changelist'), {
            'action': 'approve_users',
            '_selected_action': [str(pending.pk)],
        })

        pending.refresh_from_db()
        self.assertTrue(pending.is_approved)

    def test_a_user_can_still_be_added(self):
        response = self.client.get(reverse('admin:accounts_customuser_add'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('is_superuser', response.context['adminform'].form.fields)


class BootstrapIsPreservedTests(TestCase):
    """
    The initial administrator is created by startup.sh through
    create_superuser, which does not go through the admin at all. Removing the
    fields from the form must not have touched that.
    """

    def test_create_superuser_still_grants_authority(self):
        root = User.objects.create_superuser(
            'esc_root', email='esc_root@test.invalid', password='pw-root-1')
        self.assertTrue(root.is_superuser)
        self.assertTrue(root.is_staff)

    def test_the_shell_path_is_unaffected(self):
        """Authority is granted from the shell by design, not through the UI."""
        user = User.objects.create_user(
            'esc_shell', email='esc_shell@test.invalid', password='pw-shell-1')
        user.is_staff = True
        user.save(update_fields=['is_staff'])

        user.refresh_from_db()
        self.assertTrue(user.is_staff)

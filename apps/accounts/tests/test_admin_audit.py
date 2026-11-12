"""
F6/F7 — administrative actions are recorded, and the trail has a reader.

What was actually missing
--------------------------
Django's own `LogEntry` covers more than the earlier review credited: it records
add, change and delete performed through the admin's change form. What it does
not cover is exactly where administrative authority is exercised here —

  * custom actions. Approval, activation and rejection all use
    `queryset.update()`, which writes no LogEntry at all;
  * refusals. Nothing was written, so nothing was recorded — and a refused
    self-escalation is the row most worth having;
  * actor survival. `LogEntry.user` is CASCADE, so deleting an administrator
    erases their entire admin history, the opposite of an audit trail's purpose.

`AdminAuditEvent` covers those three and nothing else. It is not a replacement
for LogEntry and not a general event bus.

And the trail had no reader. `DoctorAccessLog` was written by four call sites
and readable only by the patient it concerned, through the data export — no
compliance review was possible without shell access.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import AdminAuditEvent, DoctorAccessLog
from apps.ai_insights.models import AIModel

User = get_user_model()
A = AdminAuditEvent.Action


class _Admin(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(
            'aud_admin', email='aud_admin@test.invalid', password='pw-admin-1')
        self.client.force_login(self.admin)

    def _action(self, url_name, action, objects):
        return self.client.post(reverse(url_name), {
            'action': action,
            '_selected_action': [str(o.pk) for o in objects],
        }, follow=True)


class UserAdministrationIsRecordedTests(_Admin):

    def _pending(self, username='aud_pending'):
        return User.objects.create_user(
            username, email=f'{username}@test.invalid', password='pw',
            role='doctor', is_approved=False)

    def test_approving_a_user_is_recorded(self):
        """ACCEPTANCE — queryset.update() writes no LogEntry."""
        user = self._pending()
        self._action('admin:accounts_customuser_changelist', 'approve_users', [user])

        event = AdminAuditEvent.objects.get(action=A.USER_APPROVED)
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.target_label, user.username)
        self.assertTrue(event.success)

    def test_each_approved_user_gets_its_own_row(self):
        """A count cannot answer 'who approved this account'."""
        users = [self._pending(f'aud_p{i}') for i in range(3)]
        self._action('admin:accounts_customuser_changelist', 'approve_users', users)

        self.assertEqual(AdminAuditEvent.objects.filter(action=A.USER_APPROVED).count(), 3)

    def test_rejecting_a_user_is_recorded_before_the_row_disappears(self):
        user = self._pending('aud_reject')
        username = user.username

        self._action('admin:accounts_customuser_changelist', 'reject_users', [user])

        event = AdminAuditEvent.objects.get(action=A.USER_REJECTED)
        self.assertEqual(event.target_label, username)
        self.assertFalse(User.objects.filter(username=username).exists())

    def test_the_authority_relied_on_is_recorded(self):
        self._action('admin:accounts_customuser_changelist', 'approve_users',
                     [self._pending('aud_auth')])
        self.assertEqual(
            AdminAuditEvent.objects.get(action=A.USER_APPROVED).authority, 'superuser')


class RefusalsAreRecordedTests(_Admin):
    """The rows worth reading: attempts that were stopped."""

    def test_a_refused_authority_change_is_recorded(self):
        """ACCEPTANCE — nothing was written, so nothing was recorded."""
        from django.contrib import admin as dj
        from django.core.exceptions import PermissionDenied

        from apps.accounts.admin import CustomUserAdmin

        victim = User.objects.create_user(
            'aud_victim', email='aud_victim@test.invalid', password='pw')
        victim.is_superuser = True

        ma = CustomUserAdmin(User, dj.site)
        request = type('R', (), {'user': self.admin})()
        with self.assertRaises(PermissionDenied):
            ma.save_model(request, victim, form=None, change=True)

        event = AdminAuditEvent.objects.get(action=A.ESCALATION_DENIED)
        self.assertFalse(event.success)
        self.assertIn('is_superuser', event.metadata['fields'])

    def test_a_self_escalation_attempt_is_marked_as_such(self):
        from django.contrib import admin as dj
        from django.core.exceptions import PermissionDenied

        from apps.accounts.admin import CustomUserAdmin

        self.admin.role = 'doctor'
        ma = CustomUserAdmin(User, dj.site)
        request = type('R', (), {'user': self.admin})()
        with self.assertRaises(PermissionDenied):
            ma.save_model(request, self.admin, form=None, change=True)

        event = AdminAuditEvent.objects.get(action=A.ESCALATION_DENIED)
        self.assertTrue(event.metadata['self_target'])


class ModelGovernanceIsRecordedTests(_Admin):

    def _model(self, status=AIModel.Status.PENDING, name='Aud'):
        return AIModel.objects.create(
            data_scientist=None, is_system=True, name=name,
            description='d', status=status)

    def test_approval_is_recorded(self):
        model = self._model()
        self._action('admin:ai_insights_aimodel_changelist', 'approve_models', [model])
        self.assertTrue(AdminAuditEvent.objects.filter(action=A.MODEL_APPROVED).exists())

    def test_activation_is_recorded(self):
        """The single most consequential administrative action here."""
        model = self._model(AIModel.Status.APPROVED)
        self._action('admin:ai_insights_aimodel_changelist', 'activate_models', [model])

        event = AdminAuditEvent.objects.get(action=A.MODEL_ACTIVATED)
        self.assertEqual(event.target_label, model.slug)

    def test_a_refused_activation_records_nothing(self):
        """It did not happen, so there is nothing to record about the model."""
        model = self._model(AIModel.Status.PENDING)
        self._action('admin:ai_insights_aimodel_changelist', 'activate_models', [model])
        self.assertFalse(AdminAuditEvent.objects.filter(action=A.MODEL_ACTIVATED).exists())

    def test_rejection_is_recorded(self):
        model = self._model()
        self._action('admin:ai_insights_aimodel_changelist', 'reject_models', [model])
        self.assertTrue(AdminAuditEvent.objects.filter(action=A.MODEL_REJECTED).exists())


class ActorSurvivesDeletionTests(_Admin):
    """LogEntry.user is CASCADE; this trail must outlive the account."""

    def test_the_actor_label_survives_deleting_the_administrator(self):
        second = User.objects.create_superuser(
            'aud_admin2', email='aud_admin2@test.invalid', password='pw-admin-2')
        self.client.force_login(second)

        user = User.objects.create_user(
            'aud_target', email='aud_target@test.invalid', password='pw',
            role='doctor', is_approved=False)
        self._action('admin:accounts_customuser_changelist', 'approve_users', [user])

        second.delete()

        event = AdminAuditEvent.objects.get(action=A.USER_APPROVED)
        self.assertIsNone(event.actor)
        self.assertIn('aud_admin2', event.actor_label)


class MetadataIsPhiSafeTests(TestCase):
    """The trail is readable by compliance staff; it must not carry content."""

    def test_a_long_string_is_redacted(self):
        from apps.accounts.audit import record

        actor = User.objects.create_superuser(
            'aud_phi', email='aud_phi@test.invalid', password='pw-1')
        record(A.USER_APPROVED, actor=actor, reason='x' * 500)

        self.assertEqual(
            AdminAuditEvent.objects.get().metadata['reason'], '<redacted:non-scalar>')

    def test_multiline_text_is_redacted(self):
        from apps.accounts.audit import record

        actor = User.objects.create_superuser(
            'aud_phi2', email='aud_phi2@test.invalid', password='pw-1')
        record(A.USER_APPROVED, actor=actor, note='Glucose 5.2\nPotassium 6.8')

        self.assertEqual(
            AdminAuditEvent.objects.get().metadata['note'], '<redacted:non-scalar>')

    def test_scalars_are_kept(self):
        from apps.accounts.audit import record

        actor = User.objects.create_superuser(
            'aud_phi3', email='aud_phi3@test.invalid', password='pw-1')
        record(A.USER_APPROVED, actor=actor, count=3, role='doctor', ok=True)

        metadata = AdminAuditEvent.objects.get().metadata
        self.assertEqual(metadata['count'], 3)
        self.assertEqual(metadata['role'], 'doctor')
        self.assertTrue(metadata['ok'])

    def test_an_audit_failure_does_not_break_the_action(self):
        """
        Administration must not become unavailable because its logging is.
        """
        from unittest.mock import patch

        from apps.accounts.audit import record

        with patch('apps.accounts.models.AdminAuditEvent.objects.create',
                   side_effect=RuntimeError('table gone')):
            with self.assertLogs('apps.accounts.audit', level='ERROR'):
                record(A.USER_APPROVED, actor=None)      # must not raise


class TheTrailHasAReaderTests(_Admin):
    """F7 — written by four call sites, previously readable by nobody."""

    def test_the_audit_event_list_is_reachable(self):
        response = self.client.get(reverse('admin:accounts_adminauditevent_changelist'))
        self.assertEqual(response.status_code, 200)

    def test_the_clinical_access_log_is_reachable(self):
        response = self.client.get(reverse('admin:accounts_doctoraccesslog_changelist'))
        self.assertEqual(response.status_code, 200)

    def test_audit_events_cannot_be_added_changed_or_deleted(self):
        """An audit trail an administrator can edit is not evidence."""
        from django.contrib import admin as dj

        from apps.accounts.admin import AdminAuditEventAdmin

        ma = AdminAuditEventAdmin(AdminAuditEvent, dj.site)
        request = type('R', (), {'user': self.admin})()

        self.assertFalse(ma.has_add_permission(request))
        self.assertFalse(ma.has_change_permission(request))
        self.assertFalse(ma.has_delete_permission(request))

    def test_the_access_log_cannot_be_edited_either(self):
        from django.contrib import admin as dj

        from apps.accounts.admin import DoctorAccessLogAdmin

        ma = DoctorAccessLogAdmin(DoctorAccessLog, dj.site)
        request = type('R', (), {'user': self.admin})()

        self.assertFalse(ma.has_add_permission(request))
        self.assertFalse(ma.has_change_permission(request))
        self.assertFalse(ma.has_delete_permission(request))

    def test_the_add_view_is_refused_over_http(self):
        response = self.client.get(reverse('admin:accounts_adminauditevent_add'))
        self.assertIn(response.status_code, (403, 302))

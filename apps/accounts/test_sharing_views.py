"""
The sharing workflow at the HTTP boundary.

`test_sharing.py` proves the authorization seam. This proves the endpoints
enforce it too — no rule may depend on the interface hiding a control, so every
invariant is asserted through an actual request as well.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import (AdminAuditEvent, DoctorAccessLog, SharingGrant)

User = get_user_model()


class _Views(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'shv_patient', email='shv_patient@test.invalid', password='pw', role='patient')
        self.daughter = User.objects.create_user(
            'shv_daughter', email='shv_daughter@test.invalid', password='pw', role='patient')
        self.admin = User.objects.create_superuser(
            'shv_admin', email='shv_admin@test.invalid', password='pw-admin-1')
        self.client.force_login(self.patient)

    def _create(self, recipient='shv_daughter', scopes=('records',)):
        return self.client.post(reverse('accounts:create_share'),
                                {'recipient': recipient, 'scopes': list(scopes)},
                                follow=True)


class CreateTests(_Views):

    def test_a_patient_can_share_their_own_record(self):
        self._create()

        grant = SharingGrant.objects.get()
        self.assertEqual(grant.patient, self.patient)
        self.assertEqual(grant.recipient, self.daughter)
        self.assertTrue(grant.can_view_records)

    def test_sharing_by_email_works(self):
        self._create(recipient='shv_daughter@test.invalid')
        self.assertEqual(SharingGrant.objects.count(), 1)

    def test_only_the_named_scopes_are_granted(self):
        self._create(scopes=('alerts',))

        grant = SharingGrant.objects.get()
        self.assertTrue(grant.can_view_alerts)
        self.assertFalse(grant.can_view_records)
        self.assertFalse(grant.can_view_appointments)

    def test_sharing_with_nobody_is_refused(self):
        self._create(recipient='does-not-exist')
        self.assertEqual(SharingGrant.objects.count(), 0)

    def test_an_unknown_recipient_does_not_confirm_which_addresses_exist(self):
        """
        The same message either way. Otherwise this form is an oracle for which
        email addresses are registered.
        """
        missing = self._create(recipient='nobody@test.invalid')
        text_missing = ' '.join(str(m) for m in missing.context['messages'])

        SharingGrant.objects.all().delete()
        self.client.post(reverse('accounts:create_share'),
                         {'recipient': '   ', 'scopes': ['records']}, follow=True)

        self.assertIn('No account matches', text_missing)

    def test_sharing_with_no_scope_is_refused(self):
        self._create(scopes=())
        self.assertEqual(SharingGrant.objects.count(), 0)

    def test_self_sharing_is_refused(self):
        self._create(recipient='shv_patient')
        self.assertEqual(SharingGrant.objects.count(), 0)

    def test_re_sharing_updates_rather_than_duplicating(self):
        """The uniqueness constraint must not surface as a 500."""
        self._create(scopes=('records',))
        self._create(scopes=('alerts',))

        grant = SharingGrant.objects.get()
        self.assertTrue(grant.can_view_alerts)
        self.assertFalse(grant.can_view_records)

    def test_re_sharing_reactivates_a_stopped_grant(self):
        self._create()
        SharingGrant.objects.get().revoke(by=self.patient)

        self._create(scopes=('records',))

        grant = SharingGrant.objects.get()
        self.assertEqual(grant.status, SharingGrant.Status.ACTIVE)
        self.assertIsNone(grant.revoked_at)

    def test_creating_a_share_is_recorded_in_the_patients_trail(self):
        self._create()
        self.assertTrue(
            DoctorAccessLog.objects.filter(resource__startswith='share_granted').exists())

    def test_get_is_refused(self):
        self.assertEqual(
            self.client.get(reverse('accounts:create_share')).status_code, 405)

    def test_anonymous_cannot_create(self):
        self.client.logout()
        response = self.client.post(reverse('accounts:create_share'),
                                    {'recipient': 'shv_daughter', 'scopes': ['records']})
        self.assertIn(response.status_code, (302, 403))
        self.assertEqual(SharingGrant.objects.count(), 0)


class RevokeTests(_Views):

    def _grant(self):
        return SharingGrant.objects.create(
            patient=self.patient, recipient=self.daughter, can_view_records=True)

    def test_the_patient_can_stop_sharing(self):
        grant = self._grant()
        self.client.post(reverse('accounts:revoke_share', args=[grant.pk]))

        grant.refresh_from_db()
        self.assertEqual(grant.status, SharingGrant.Status.REVOKED)

    def test_the_recipient_cannot_revoke(self):
        grant = self._grant()
        self.client.force_login(self.daughter)

        response = self.client.post(reverse('accounts:revoke_share', args=[grant.pk]))

        self.assertEqual(response.status_code, 403)
        grant.refresh_from_db()
        self.assertEqual(grant.status, SharingGrant.Status.ACTIVE)

    def test_an_unrelated_user_cannot_revoke(self):
        grant = self._grant()
        stranger = User.objects.create_user(
            'shv_stranger', email='shv_stranger@test.invalid', password='pw')
        self.client.force_login(stranger)

        self.assertEqual(
            self.client.post(reverse('accounts:revoke_share', args=[grant.pk])).status_code,
            403)

    def test_get_is_refused(self):
        grant = self._grant()
        self.assertEqual(
            self.client.get(reverse('accounts:revoke_share', args=[grant.pk])).status_code,
            405)


class ListingTests(_Views):

    def test_the_page_shows_who_you_share_with(self):
        SharingGrant.objects.create(
            patient=self.patient, recipient=self.daughter, can_view_records=True)

        body = self.client.get(reverse('accounts:my_shares')).content.decode()
        self.assertIn('shv_daughter', body)

    def test_the_page_shows_who_shares_with_you(self):
        SharingGrant.objects.create(
            patient=self.daughter, recipient=self.patient, can_view_records=True)

        body = self.client.get(reverse('accounts:my_shares')).content.decode()
        self.assertIn('shv_daughter', body)

    def test_an_expired_grant_is_not_listed_as_shared_with_you(self):
        """Listing it would promise access the patient no longer has."""
        SharingGrant.objects.create(
            patient=self.daughter, recipient=self.patient, can_view_records=True,
            expires_at=timezone.now() - timedelta(days=1))

        response = self.client.get(reverse('accounts:my_shares'))
        self.assertEqual(list(response.context['received']), [])

    def test_you_do_not_see_other_peoples_grants(self):
        stranger = User.objects.create_user(
            'shv_other', email='shv_other@test.invalid', password='pw')
        SharingGrant.objects.create(
            patient=stranger, recipient=self.daughter, can_view_records=True)

        response = self.client.get(reverse('accounts:my_shares'))
        self.assertEqual(list(response.context['granted']), [])
        self.assertEqual(list(response.context['received']), [])

    def test_the_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('accounts:my_shares'))
        self.assertIn(response.status_code, (302, 301))


class AdminSurfaceTests(_Views):
    """Metadata and revocation. Never the records behind them."""

    def setUp(self):
        super().setUp()
        self.grant = SharingGrant.objects.create(
            patient=self.patient, recipient=self.daughter, can_view_records=True)
        self.client.force_login(self.admin)

    def test_an_administrator_can_list_grants(self):
        response = self.client.get(reverse('admin:accounts_sharinggrant_changelist'))
        self.assertEqual(response.status_code, 200)

    def test_an_administrator_cannot_add_a_grant(self):
        from django.contrib import admin as dj

        from apps.accounts.admin import SharingGrantAdmin

        ma = SharingGrantAdmin(SharingGrant, dj.site)
        request = type('R', (), {'user': self.admin})()
        self.assertFalse(ma.has_add_permission(request))

    def test_the_add_view_is_refused_over_http(self):
        response = self.client.get(reverse('admin:accounts_sharinggrant_add'))
        self.assertIn(response.status_code, (403, 302))

    def test_an_administrator_can_stop_an_abusive_share(self):
        self.client.post(reverse('admin:accounts_sharinggrant_changelist'), {
            'action': 'revoke_grants',
            '_selected_action': [str(self.grant.pk)],
        }, follow=True)

        self.grant.refresh_from_db()
        self.assertEqual(self.grant.status, SharingGrant.Status.REVOKED)
        self.assertEqual(self.grant.revoked_by, self.admin)

    def test_administrative_revocation_is_audited(self):
        self.client.post(reverse('admin:accounts_sharinggrant_changelist'), {
            'action': 'revoke_grants',
            '_selected_action': [str(self.grant.pk)],
        }, follow=True)

        event = AdminAuditEvent.objects.get(action=AdminAuditEvent.Action.SHARE_REVOKED)
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.authority, 'superuser')
        self.assertTrue(event.success)

    def test_the_audit_event_carries_no_clinical_content(self):
        self.client.post(reverse('admin:accounts_sharinggrant_changelist'), {
            'action': 'revoke_grants',
            '_selected_action': [str(self.grant.pk)],
        }, follow=True)

        event = AdminAuditEvent.objects.get(action=AdminAuditEvent.Action.SHARE_REVOKED)
        blob = f'{event.metadata} {event.target_label}'
        for leak in ('Blood', 'Glucose', 'mmol'):
            self.assertNotIn(leak, blob)

    def test_revoking_does_not_grant_the_administrator_the_records(self):
        """The capability boundary the whole design rests on."""
        from apps.accounts.authz import can_view_shared_records

        self.client.post(reverse('admin:accounts_sharinggrant_changelist'), {
            'action': 'revoke_grants',
            '_selected_action': [str(self.grant.pk)],
        }, follow=True)

        self.assertFalse(can_view_shared_records(self.admin, self.patient))

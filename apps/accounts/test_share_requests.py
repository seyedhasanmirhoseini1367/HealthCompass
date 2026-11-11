"""
Asking someone to share with you.

A request is never access: the patient approves on Companions and picks the
scopes. These are asserted through real requests, like the rest of sharing.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.authz import sharing_grant
from apps.accounts.models import SharingGrant, ShareRequest
from apps.notifications.models import Notification

User = get_user_model()


class _Base(TestCase):

    def setUp(self):
        self.mother = User.objects.create_user(
            'sr_mother', email='sr_mother@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.son = User.objects.create_user(
            'sr_son', email='sr_son@test.invalid', password='pw',
            role='patient', first_name='Mikko')
        self.stranger = User.objects.create_user(
            'sr_stranger', email='sr_stranger@test.invalid', password='pw')

    def _ask(self, who='sr_mother', message='So I can help'):
        self.client.force_login(self.son)
        return self.client.post(reverse('accounts:request_share'),
                                {'patient': who, 'message': message})

    def _answer(self, req, decision, scopes=()):
        self.client.force_login(self.mother)
        return self.client.post(reverse('accounts:answer_share_request', args=[req.pk]),
                                {'decision': decision, 'scopes': list(scopes)})


class RequestTests(_Base):

    def test_asking_creates_a_request_not_access(self):
        self._ask()
        req = ShareRequest.objects.get()
        self.assertEqual((req.requester, req.patient, req.status),
                         (self.son, self.mother, ShareRequest.Status.PENDING))
        self.assertFalse(SharingGrant.objects.exists())
        self.assertIsNone(sharing_grant(self.son, self.mother, 'alerts'))

    def test_the_patient_is_notified_and_sees_it_on_companions(self):
        self._ask()
        self.assertTrue(Notification.objects.filter(user=self.mother,
                                                    link='/care/#sharing').exists())
        self.client.force_login(self.mother)
        response = self.client.get('/care/')
        self.assertContains(response, 'Requests for you')
        self.assertContains(response, 'So I can help')

    def test_asking_twice_does_not_create_a_second_request(self):
        self._ask()
        self._ask()
        self.assertEqual(ShareRequest.objects.count(), 1)

    def test_unknown_account_and_self_are_refused(self):
        self._ask(who='nobody_here')
        self._ask(who='sr_son')
        self.assertFalse(ShareRequest.objects.exists())

    def test_already_shared_is_not_asked_again(self):
        SharingGrant.objects.create(patient=self.mother, recipient=self.son,
                                    can_view_alerts=True)
        self._ask()
        self.assertFalse(ShareRequest.objects.exists())

    def test_requires_post_and_login(self):
        self.client.force_login(self.son)
        self.assertEqual(self.client.get(reverse('accounts:request_share')).status_code, 405)
        self.client.logout()
        self.client.post(reverse('accounts:request_share'), {'patient': 'sr_mother'})
        self.assertFalse(ShareRequest.objects.exists())


class AnswerTests(_Base):

    def setUp(self):
        super().setUp()
        self._ask()
        self.req = ShareRequest.objects.get()

    def test_approving_grants_exactly_the_chosen_scopes(self):
        self._answer(self.req, 'approve', scopes=['alerts', 'appointments'])
        grant = SharingGrant.objects.get(patient=self.mother, recipient=self.son)
        self.assertTrue(grant.can_view_alerts)
        self.assertTrue(grant.can_view_appointments)
        self.assertFalse(grant.can_view_records)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, ShareRequest.Status.APPROVED)
        self.assertTrue(Notification.objects.filter(user=self.son).exists())

    def test_approving_with_nothing_chosen_shares_nothing(self):
        self._answer(self.req, 'approve', scopes=[])
        self.assertFalse(SharingGrant.objects.exists())
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, ShareRequest.Status.PENDING)

    def test_unknown_scopes_are_ignored(self):
        self._answer(self.req, 'approve', scopes=['everything'])
        self.assertFalse(SharingGrant.objects.exists())

    def test_declining_shares_nothing_and_does_not_notify(self):
        self._answer(self.req, 'decline')
        self.assertFalse(SharingGrant.objects.exists())
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, ShareRequest.Status.DECLINED)
        self.assertFalse(Notification.objects.filter(user=self.son).exists())

    def test_a_decline_blocks_repeat_requests_for_a_while(self):
        self._answer(self.req, 'decline')
        self._ask()
        self.assertEqual(ShareRequest.objects.count(), 1)

        ShareRequest.objects.update(responded_at=timezone.now() - timedelta(
            days=ShareRequest.DECLINE_COOLDOWN_DAYS + 1))
        self._ask()
        self.assertEqual(ShareRequest.objects.filter(
            status=ShareRequest.Status.PENDING).count(), 1)

    def test_only_the_patient_can_answer(self):
        for user in (self.son, self.stranger):
            self.client.force_login(user)
            response = self.client.post(
                reverse('accounts:answer_share_request', args=[self.req.pk]),
                {'decision': 'approve', 'scopes': ['records']})
            self.assertEqual(response.status_code, 404)
        self.assertFalse(SharingGrant.objects.exists())

    def test_an_answered_request_cannot_be_answered_again(self):
        self._answer(self.req, 'decline')
        response = self._answer(self.req, 'approve', scopes=['records'])
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SharingGrant.objects.exists())

    def test_sharing_directly_closes_the_open_request(self):
        self.client.force_login(self.mother)
        self.client.post(reverse('accounts:create_share'),
                         {'recipient': 'sr_son', 'scopes': ['alerts']})
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, ShareRequest.Status.APPROVED)


class CancelTests(_Base):

    def test_the_requester_can_cancel_and_nobody_else(self):
        self._ask()
        req = ShareRequest.objects.get()

        self.client.force_login(self.stranger)
        self.assertEqual(self.client.post(
            reverse('accounts:cancel_share_request', args=[req.pk])).status_code, 404)

        self.client.force_login(self.son)
        self.client.post(reverse('accounts:cancel_share_request', args=[req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.status, ShareRequest.Status.CANCELLED)

    def test_the_requester_sees_their_pending_request(self):
        self._ask()
        self.client.force_login(self.son)
        response = self.client.get('/care/')
        self.assertContains(response, 'Requests I sent')
        self.assertContains(response, 'Aino')


class CompanionsPageTests(_Base):

    def test_stop_sharing_and_share_form_are_on_companions(self):
        grant = SharingGrant.objects.create(patient=self.mother, recipient=self.son,
                                            can_view_alerts=True)
        self.client.force_login(self.mother)
        response = self.client.get('/care/')
        self.assertContains(response, reverse('accounts:revoke_share', args=[grant.pk]))
        self.assertContains(response, reverse('accounts:create_share'))

        response = self.client.post(reverse('accounts:revoke_share', args=[grant.pk]))
        self.assertRedirects(response, '/care/#sharing', fetch_redirect_response=False)

    def test_someone_sharing_only_records_is_listed(self):
        SharingGrant.objects.create(patient=self.mother, recipient=self.son,
                                    can_view_records=True)
        self.client.force_login(self.son)
        response = self.client.get('/care/')
        self.assertContains(response, 'Aino')
        self.assertContains(response, 'Shares their')

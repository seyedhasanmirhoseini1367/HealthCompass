"""Two-step sharing (audit SEC-02, SEC-14, prompt 4 task 2)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.authz import sharing_grant
from apps.accounts.models import ShareRequest, SharingGrant
from apps.notifications.models import Notification

User = get_user_model()


class InvitationTests(TestCase):
    def setUp(self):
        self.patient = User.objects.create_user('inv_p', email='p@test.invalid', password='pw')
        self.friend = User.objects.create_user('inv_f', email='f@test.invalid', password='pw')
        self.client.force_login(self.patient)

    def _share(self, email='f@test.invalid'):
        return self.client.post(reverse('accounts:create_share'),
                                {'recipient': email, 'scopes': ['records']}, follow=True)

    def test_a_new_share_gives_nothing_until_accepted(self):
        self._share()
        grant = SharingGrant.objects.get()
        self.assertEqual(grant.status, SharingGrant.Status.PENDING)
        self.assertIsNone(sharing_grant(self.friend, self.patient, 'records'))
        self.assertTrue(Notification.objects.filter(user=self.friend).exists())

        self.client.force_login(self.friend)
        self.client.post(reverse('accounts:accept_share', args=[grant.pk]))
        self.assertIsNotNone(sharing_grant(self.friend, self.patient, 'records'))

    def test_decline_removes_the_invitation(self):
        self._share()
        grant = SharingGrant.objects.get()
        self.client.force_login(self.friend)
        self.client.post(reverse('accounts:decline_share', args=[grant.pk]))
        self.assertFalse(SharingGrant.objects.exists())

    def test_only_the_recipient_can_accept(self):
        self._share()
        grant = SharingGrant.objects.get()
        stranger = User.objects.create_user('inv_s', email='s@test.invalid', password='pw')
        self.client.force_login(stranger)
        self.client.post(reverse('accounts:accept_share', args=[grant.pk]))
        grant.refresh_from_db()
        self.assertEqual(grant.status, SharingGrant.Status.PENDING)

    def test_answering_their_own_request_activates_directly(self):
        ShareRequest.objects.create(requester=self.friend, patient=self.patient)
        self._share()
        self.assertEqual(SharingGrant.objects.get().status, SharingGrant.Status.ACTIVE)

    def test_reply_is_the_same_for_unknown_addresses(self):
        known = ' '.join(str(m) for m in self._share('f@test.invalid').context['messages'])
        unknown = ' '.join(str(m) for m in self._share('ghost@test.invalid').context['messages'])
        self.assertEqual(known.replace('f@test.invalid', 'X'), unknown.replace('ghost@test.invalid', 'X'))

    def test_companions_page_lists_the_invitation_for_the_recipient(self):
        self._share()
        self.client.force_login(self.friend)
        self.assertContains(self.client.get('/care/'), 'Invitations for you')

    def test_api_accept_and_neutral_create(self):
        api = APIClient()
        api.force_authenticate(self.patient)
        a = api.post('/api/v1/sharing/companions/create/',
                     {'identifier': 'f@test.invalid', 'scopes': ['records']}, format='json')
        b = api.post('/api/v1/sharing/companions/create/',
                     {'identifier': 'ghost@test.invalid', 'scopes': ['records']}, format='json')
        self.assertEqual(a.status_code, b.status_code)
        grant = SharingGrant.objects.get()
        api.force_authenticate(self.friend)
        self.assertEqual(api.post(f'/api/v1/sharing/companions/{grant.pk}/accept/').status_code, 200)
        grant.refresh_from_db()
        self.assertEqual(grant.status, SharingGrant.Status.ACTIVE)

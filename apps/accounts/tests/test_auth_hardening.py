"""Prompt 2 of the production-readiness audit: one set of account rules for web
and API (SEC-08), revocable JWTs (SEC-04), email verification and verified
email change (SEC-05), deletion for every account type (SEC-13) and MFA for
privileged roles (SEC-17)."""
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase as _TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts import auth_service
from apps.accounts.models import SharingGrant

User = get_user_model()
STRONG = 'Correct-Horse-Battery-77'
MANDATORY = override_settings(ACCOUNT_EMAIL_VERIFICATION='mandatory')


class TestCase(_TestCase):
    """Throttle counters live in the cache; start every test from zero."""

    def setUp(self):
        cache.clear()
        super().setUp()


def _api_login(email, password):
    return APIClient().post('/api/v1/auth/login/', {'email': email, 'password': password},
                            format='json')


class WebApiParityTests(TestCase):
    """The same input gets the same outcome on both surfaces."""

    def test_weak_password_is_refused_by_both(self):
        for weak in ('password', '12345678'):
            with self.subTest(surface='web', password=weak):
                self.client.post(reverse('accounts:register'), {
                    'username': 'weakweb', 'email': f'web_{weak}@test.invalid',
                    'password1': weak, 'password2': weak})
                self.assertFalse(User.objects.filter(email=f'web_{weak}@test.invalid').exists())
            with self.subTest(surface='api', password=weak):
                response = APIClient().post('/api/v1/auth/register/', {
                    'email': f'api_{weak}@test.invalid', 'password': weak, 'password2': weak},
                    format='json')
                self.assertEqual(response.status_code, 400)
                self.assertFalse(User.objects.filter(email=f'api_{weak}@test.invalid').exists())

    def test_api_change_password_applies_the_validators(self):
        user = User.objects.create_user('cp', email='cp@test.invalid', password=STRONG)
        api = APIClient()
        api.force_authenticate(user)
        response = api.post('/api/v1/auth/change-password/',
                            {'old_password': STRONG, 'new_password': '12345678'}, format='json')
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertTrue(user.check_password(STRONG))

    def test_unapproved_account_is_refused_by_both_with_the_same_wording(self):
        User.objects.create_user('pending_doc', email='doc@test.invalid', password=STRONG,
                                 role='doctor', is_approved=False)
        web = self.client.post(reverse('accounts:login'),
                               {'username': 'doc@test.invalid', 'password': STRONG}, follow=True)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertIn(auth_service.MSG_PENDING, web.content.decode())

        api = _api_login('doc@test.invalid', STRONG)
        self.assertEqual(api.status_code, 403)
        self.assertEqual(api.json()['error'], auth_service.MSG_PENDING)
        self.assertNotIn('access', api.json())

    def test_wrong_password_is_refused_by_both(self):
        User.objects.create_user('wp', email='wp@test.invalid', password=STRONG)
        self.client.post(reverse('accounts:login'), {'username': 'wp@test.invalid', 'password': 'nope'})
        self.assertNotIn('_auth_user_id', self.client.session)
        response = _api_login('wp@test.invalid', 'nope')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error'], auth_service.MSG_INVALID)

    def test_unknown_account_and_wrong_password_look_the_same_on_the_api(self):
        User.objects.create_user('known', email='known@test.invalid', password=STRONG)
        a, b = _api_login('known@test.invalid', 'nope'), _api_login('ghost@test.invalid', 'nope')
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))


class RevocableTokenTests(TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('jwt', email='jwt@test.invalid', password=STRONG)

    def _tokens(self):
        return _api_login('jwt@test.invalid', STRONG).json()

    def _refresh(self, refresh):
        return APIClient().post('/api/v1/auth/refresh/', {'refresh': refresh}, format='json')

    def test_a_rotated_refresh_token_is_rejected(self):
        first = self._tokens()['refresh']
        self.assertEqual(self._refresh(first).status_code, 200)
        self.assertEqual(self._refresh(first).status_code, 401)

    def test_change_password_revokes_old_refresh_tokens(self):
        old = self._tokens()
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {old["access"]}')
        response = api.post('/api/v1/auth/change-password/',
                            {'old_password': STRONG, 'new_password': 'Another-Strong-Pass-88'},
                            format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._refresh(old['refresh']).status_code, 401)
        self.assertEqual(self._refresh(response.json()['refresh']).status_code, 200)

    def test_web_password_change_revokes_api_tokens(self):
        old = self._tokens()
        self.client.force_login(self.user)
        self.client.post(reverse('accounts:change_password'), {
            'old_password': STRONG, 'new_password1': 'Another-Strong-Pass-88',
            'new_password2': 'Another-Strong-Pass-88'})
        self.assertEqual(self._refresh(old['refresh']).status_code, 401)

    def test_logout_blacklists_the_refresh_token_and_drops_the_device(self):
        from apps.notifications.models import FCMDevice
        tokens = self._tokens()
        FCMDevice.objects.create(user=self.user, token='device-1')
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}')
        response = api.post('/api/v1/auth/logout/',
                            {'refresh': tokens['refresh'], 'fcm_token': 'device-1'}, format='json')
        self.assertEqual(response.status_code, 205)
        self.assertEqual(self._refresh(tokens['refresh']).status_code, 401)
        self.assertFalse(FCMDevice.objects.filter(token='device-1').exists())

    def test_logout_all_invalidates_a_second_device(self):
        phone, tablet = self._tokens(), self._tokens()
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {phone["access"]}')
        self.assertEqual(api.post('/api/v1/auth/logout-all/').status_code, 200)
        self.assertEqual(self._refresh(tablet['refresh']).status_code, 401)


@MANDATORY
class EmailVerificationTests(TestCase):
    def test_web_registration_sends_one_confirmation_mail(self):
        self.client.post(reverse('accounts:register'), {
            'username': 'newweb', 'email': 'newweb@test.invalid',
            'password1': STRONG, 'password2': STRONG})
        self.assertTrue(User.objects.filter(email='newweb@test.invalid').exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['newweb@test.invalid'])

    def test_api_registration_sends_one_confirmation_mail(self):
        response = APIClient().post('/api/v1/auth/register/', {
            'email': 'newapi@test.invalid', 'password': STRONG, 'password2': STRONG}, format='json')
        # 202 and no tokens: with verification on, the answer is the same for a
        # taken address (prompt 5, SEC-14).
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(len(mail.outbox), 1)

    def test_unverified_user_cannot_share_or_ask(self):
        user = auth_service.register_patient('unv@test.invalid', STRONG)
        User.objects.create_user('friend', email='friend@test.invalid', password=STRONG)
        self.client.force_login(user)
        self.client.post(reverse('accounts:create_share'),
                         {'recipient': 'friend@test.invalid', 'scopes': ['records']})
        self.assertFalse(SharingGrant.objects.exists())
        api = APIClient()
        api.force_authenticate(user)
        response = api.post('/api/v1/assistant/ask/', {'query': 'hello'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'email_unverified')

    def test_verified_user_can_share(self):
        user = auth_service.register_patient('ver@test.invalid', STRONG)
        auth_service.mark_email_verified(user)
        User.objects.create_user('friend2', email='friend2@test.invalid', password=STRONG)
        self.client.force_login(user)
        self.client.post(reverse('accounts:create_share'),
                         {'recipient': 'friend2@test.invalid', 'scopes': ['records']})
        self.assertTrue(SharingGrant.objects.exists())

    def test_banner_shows_only_while_unverified(self):
        user = auth_service.register_patient('ban@test.invalid', STRONG)
        self.client.force_login(user)
        self.assertContains(self.client.get('/accounts/profile/'), 'verify-banner')
        auth_service.mark_email_verified(user)
        self.assertNotContains(self.client.get('/accounts/profile/'), 'verify-banner')

    def test_password_reset_marks_the_address_verified(self):
        """Which in turn lets a locally registered user link Google."""
        from apps.accounts.views import _email_is_verified
        user = auth_service.register_patient('reset@test.invalid', STRONG)
        self.assertFalse(_email_is_verified(user, user.email))
        mail.outbox.clear()
        self.client.post(reverse('accounts:password_reset'), {'email': 'reset@test.invalid'})
        link = re.search(r'/accounts/password/reset/[^/\s]+/[^/\s]+/', mail.outbox[-1].body).group(0)
        form_page = self.client.get(link, follow=True)
        self.client.post(form_page.redirect_chain[-1][0] if form_page.redirect_chain else link, {
            'new_password1': 'Brand-New-Pass-55', 'new_password2': 'Brand-New-Pass-55'})
        user.refresh_from_db()
        self.assertTrue(user.check_password('Brand-New-Pass-55'))
        self.assertTrue(_email_is_verified(user, user.email))


class EmailChangeTests(TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('chg', email='old@test.invalid', password=STRONG)
        self.client.force_login(self.user)

    def _request(self, new='new@test.invalid', password=STRONG):
        return self.client.post(reverse('accounts:email_change'),
                                {'new_email': new, 'password': password})

    def test_wrong_password_sends_nothing(self):
        self._request(password='nope')
        self.assertEqual(len(mail.outbox), 0)

    def test_email_is_unchanged_until_the_link_is_confirmed(self):
        self._request()
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@test.invalid')
        self.assertEqual(mail.outbox[0].to, ['new@test.invalid'])
        link = re.search(r'/accounts/email/change/[^\s]+/', mail.outbox[0].body).group(0)
        self.client.get(link)  # a GET (e.g. a mail scanner) changes nothing
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'old@test.invalid')
        self.client.post(link)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new@test.invalid')
        self.assertEqual(mail.outbox[-1].to, ['old@test.invalid'])  # old address notified
        self.assertTrue(auth_service.email_verified(self.user))

    def test_a_taken_address_gets_the_same_response_and_no_mail(self):
        User.objects.create_user('taken', email='taken@test.invalid', password=STRONG)
        response = self._request(new='taken@test.invalid')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)


class AccountDeletionTests(TestCase):
    def test_a_social_only_user_can_delete_through_an_emailed_link(self):
        user = User.objects.create_user('google_only', email='g@test.invalid')
        user.set_unusable_password()
        user.save()
        self.client.force_login(user)
        self.client.post(reverse('accounts:delete_account'), {'confirm_email': 'g@test.invalid'})
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        link = re.search(r'/accounts/delete/confirm/[^\s]+/', mail.outbox[0].body).group(0)
        self.client.post(link)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())

    def test_a_wrong_email_sends_no_link(self):
        user = User.objects.create_user('google_only2', email='g2@test.invalid')
        user.set_unusable_password()
        user.save()
        self.client.force_login(user)
        self.client.post(reverse('accounts:delete_account'), {'confirm_email': 'other@test.invalid'})
        self.assertEqual(len(mail.outbox), 0)

    def test_api_delete_with_password_returns_204_and_tokens_stop_working(self):
        User.objects.create_user('apidel', email='apidel@test.invalid', password=STRONG)
        tokens = _api_login('apidel@test.invalid', STRONG).json()
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}')
        self.assertEqual(api.delete('/api/v1/auth/account/', {'password': 'nope'},
                                    format='json').status_code, 403)
        self.assertEqual(api.delete('/api/v1/auth/account/', {'password': STRONG},
                                    format='json').status_code, 204)
        self.assertFalse(User.objects.filter(email='apidel@test.invalid').exists())
        refreshed = APIClient().post('/api/v1/auth/refresh/',
                                     {'refresh': tokens['refresh']}, format='json')
        self.assertEqual(refreshed.status_code, 401)

    def test_api_delete_for_a_social_only_user_uses_the_emailed_token(self):
        from apps.accounts.security_views import make_delete_token
        user = User.objects.create_user('apisoc', email='apisoc@test.invalid')
        user.set_unusable_password()
        user.save()
        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.post('/api/v1/auth/account/delete-request/').status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(api.delete('/api/v1/auth/account/', {'token': 'bogus'},
                                    format='json').status_code, 403)
        self.assertEqual(api.delete('/api/v1/auth/account/', {'token': make_delete_token(user)},
                                    format='json').status_code, 204)


@override_settings(MFA_ENFORCED=True)
class MFATests(TestCase):
    def test_staff_without_mfa_is_sent_to_enrolment_from_admin(self):
        staff = User.objects.create_superuser('mfa_staff', email='staff@test.invalid', password=STRONG)
        self.client.force_login(staff)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('mfa_activate_totp'), response['Location'])

    def test_a_clinician_is_sent_to_enrolment(self):
        doctor = User.objects.create_user('mfa_doc', email='doc2@test.invalid', password=STRONG,
                                          role='doctor')
        self.client.force_login(doctor)
        response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('mfa_activate_totp'), response['Location'])

    def test_a_patient_is_not(self):
        patient = User.objects.create_user('mfa_pat', email='pat@test.invalid', password=STRONG)
        self.client.force_login(patient)
        response = self.client.get('/accounts/profile/')
        self.assertEqual(response.status_code, 200)

    def test_an_enrolled_user_must_enter_the_code_at_login(self):
        from allauth.mfa.models import Authenticator
        user = User.objects.create_user('mfa_enrolled', email='enr@test.invalid', password=STRONG)
        Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP,
                                     data={'secret': 'JBSWY3DPEHPK3PXP'})
        response = self.client.post(reverse('accounts:login'),
                                    {'username': 'enr@test.invalid', 'password': STRONG})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/2fa/', response['Location'])
        self.assertNotIn('_auth_user_id', self.client.session)

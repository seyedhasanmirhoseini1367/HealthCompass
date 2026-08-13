"""
Security regression tests for the mobile API.

Three concerns, kept in one place so a reviewer can see the whole security
surface of /api/v1/ at once:

  1. Object-level authorization (IDOR) — User A must never reach User B's data.
  2. Privilege escalation — self-service signup must not grant a staff role.
  3. Throttling — auth, upload and AI endpoints must return 429 when abused.

Every IDOR test asserts a *negative*: the response must not contain B's data and
must not mutate it. Tests that only assert a 404 status would still pass if the
view leaked data in the body, so the body is checked too.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle

from apps.ai_insights.models import AIModel, HealthAlert, ModelPrediction
from apps.appointments.models import Appointment
from apps.medical_records.models import MedicalRecord
from apps.notifications.models import Notification
from apps.rag_assistant.models import ChatSession, QueryLog

User = get_user_model()

# Throttling is exercised deliberately in ThrottleTests; everywhere else it would
# just make unrelated tests flaky once they exceed a per-minute budget.
NO_THROTTLE = {
    'DEFAULT_THROTTLE_CLASSES': (),
    'DEFAULT_THROTTLE_RATES': {},
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': ('rest_framework.permissions.IsAuthenticated',),
    'DEFAULT_RENDERER_CLASSES': ('rest_framework.renderers.JSONRenderer',),
}


class _TwoUserMixin:
    """Victim (B) owns data; attacker (A) is a normal authenticated user."""

    def setUp(self):
        cache.clear()
        self.attacker = User.objects.create_user(
            username='attacker', email='attacker@example.com', password='pw-attacker-1',
        )
        self.victim = User.objects.create_user(
            username='victim', email='victim@example.com', password='pw-victim-1',
        )
        self.client.force_authenticate(user=self.attacker)

    def assertDeniedAndUnchanged(self, response, *, secret):
        """Denied with 403/404, and the victim's content never appears in the body."""
        self.assertIn(response.status_code, (403, 404),
                      f'expected denial, got {response.status_code}: {response.content[:300]}')
        self.assertNotIn(secret, response.content.decode())


@override_settings(REST_FRAMEWORK=NO_THROTTLE)
class MedicalRecordIDORTests(_TwoUserMixin, APITestCase):

    def setUp(self):
        super().setUp()
        self.record = MedicalRecord.objects.create(
            patient=self.victim, title='Victim Bloodwork',
            record_type='lab_result', raw_text='CREATININE 250 CRITICAL',
        )

    def test_cannot_read_another_users_record(self):
        resp = self.client.get(f'/api/v1/records/{self.record.pk}/')
        self.assertDeniedAndUnchanged(resp, secret='Victim Bloodwork')

    def test_cannot_delete_another_users_record(self):
        resp = self.client.delete(f'/api/v1/records/{self.record.pk}/delete/')
        self.assertIn(resp.status_code, (403, 404))
        self.assertTrue(MedicalRecord.objects.filter(pk=self.record.pk).exists())

    def test_list_excludes_other_users_records(self):
        resp = self.client.get('/api/v1/records/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('Victim Bloodwork', resp.content.decode())
        self.assertEqual(resp.json(), [])

    def test_search_filter_cannot_reach_other_users_records(self):
        """A filter parameter must narrow the owner's set, never widen it."""
        resp = self.client.get('/api/v1/records/?q=Victim')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_owner_can_still_read_own_record(self):
        """Authorization fix must not break the legitimate path."""
        self.client.force_authenticate(user=self.victim)
        resp = self.client.get(f'/api/v1/records/{self.record.pk}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['title'], 'Victim Bloodwork')


@override_settings(REST_FRAMEWORK=NO_THROTTLE)
class ChatSessionIDORTests(_TwoUserMixin, APITestCase):

    def setUp(self):
        super().setUp()
        self.session = ChatSession.objects.create(patient=self.victim, title='Victim private chat')
        QueryLog.objects.create(
            session=self.session, query='Do I have cancer?',
            response='Your biopsy result says ...', sources=[],
        )

    def test_cannot_read_another_users_conversation(self):
        resp = self.client.get(f'/api/v1/assistant/sessions/{self.session.pk}/')
        self.assertDeniedAndUnchanged(resp, secret='biopsy')

    def test_cannot_rename_another_users_conversation(self):
        resp = self.client.patch(
            f'/api/v1/assistant/sessions/{self.session.pk}/', {'title': 'pwned'}, format='json',
        )
        self.assertIn(resp.status_code, (403, 404))
        self.session.refresh_from_db()
        self.assertEqual(self.session.title, 'Victim private chat')

    def test_cannot_delete_another_users_conversation(self):
        resp = self.client.delete(f'/api/v1/assistant/sessions/{self.session.pk}/')
        self.assertIn(resp.status_code, (403, 404))
        self.assertTrue(ChatSession.objects.filter(pk=self.session.pk).exists())

    def test_session_list_excludes_other_users_sessions(self):
        resp = self.client.get('/api/v1/assistant/sessions/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['sessions'], [])

    def test_posting_to_another_users_session_does_not_write_into_it(self):
        """
        A foreign session_id must not append to the victim's history. The view
        silently starts a new session for the attacker instead of erroring, so
        assert on the data, not the status code.
        """
        before = self.session.messages.count()
        self.client.post(
            '/api/v1/assistant/ask/',
            {'query': 'hello', 'session_id': str(self.session.pk)}, format='json',
        )
        self.assertEqual(self.session.messages.count(), before)


@override_settings(REST_FRAMEWORK=NO_THROTTLE)
class OwnedResourceIDORTests(_TwoUserMixin, APITestCase):
    """Appointments, alerts, notifications, predictions."""

    def setUp(self):
        super().setUp()
        from django.utils import timezone
        from datetime import timedelta

        self.appointment = Appointment.objects.create(
            patient=self.victim, title='Oncology consult',
            appointment_datetime=timezone.now() + timedelta(days=3),
        )
        self.alert = HealthAlert.objects.create(
            patient=self.victim, severity='critical',
            title='Critical potassium', message='K+ 6.8 mmol/L',
        )
        self.notification = Notification.objects.create(
            user=self.victim, title='Victim notification', message='private',
        )
        ai_model = AIModel.objects.create(
            data_scientist=self.victim, name='Risk Model',
            description='d', status='active',
        )
        self.prediction = ModelPrediction.objects.create(
            model=ai_model, patient=self.victim,
            input_data={}, result={'label': 'high risk'}, risk_score=0.91,
        )

    def test_cannot_read_another_users_appointment(self):
        resp = self.client.get(f'/api/v1/appointments/{self.appointment.pk}/')
        self.assertDeniedAndUnchanged(resp, secret='Oncology consult')

    def test_cannot_modify_another_users_appointment(self):
        resp = self.client.patch(
            f'/api/v1/appointments/{self.appointment.pk}/', {'title': 'pwned'}, format='json',
        )
        self.assertIn(resp.status_code, (403, 404))
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.title, 'Oncology consult')

    def test_cannot_delete_another_users_appointment(self):
        resp = self.client.delete(f'/api/v1/appointments/{self.appointment.pk}/')
        self.assertIn(resp.status_code, (403, 404))
        self.assertTrue(Appointment.objects.filter(pk=self.appointment.pk).exists())

    def test_cannot_mark_another_users_alert_read(self):
        resp = self.client.patch(f'/api/v1/alerts/{self.alert.pk}/read/')
        self.assertIn(resp.status_code, (403, 404))
        self.alert.refresh_from_db()
        self.assertFalse(self.alert.is_read)

    def test_cannot_mark_another_users_notification_read(self):
        resp = self.client.patch(f'/api/v1/notifications/{self.notification.pk}/read/')
        self.assertIn(resp.status_code, (403, 404))
        self.notification.refresh_from_db()
        self.assertFalse(self.notification.is_read)

    def test_cannot_read_another_users_prediction(self):
        resp = self.client.get(f'/api/v1/predictions/{self.prediction.pk}/')
        self.assertDeniedAndUnchanged(resp, secret='high risk')

    def test_listing_endpoints_are_scoped_to_the_caller(self):
        for url in ('/api/v1/appointments/', '/api/v1/alerts/',
                    '/api/v1/notifications/', '/api/v1/predictions/'):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                body = resp.content.decode()
                for secret in ('Oncology consult', 'Critical potassium',
                               'Victim notification', 'high risk'):
                    self.assertNotIn(secret, body)


@override_settings(REST_FRAMEWORK=NO_THROTTLE)
class UnauthenticatedAccessTests(APITestCase):
    """Every user-owned endpoint must reject anonymous callers."""

    def setUp(self):
        cache.clear()

    def test_protected_endpoints_require_authentication(self):
        for method, url in [
            ('get',    '/api/v1/records/'),
            ('get',    '/api/v1/auth/me/'),
            ('get',    '/api/v1/dashboard/'),
            ('get',    '/api/v1/analytics/'),
            ('get',    '/api/v1/appointments/'),
            ('get',    '/api/v1/alerts/'),
            ('get',    '/api/v1/notifications/'),
            ('get',    '/api/v1/predictions/'),
            ('get',    '/api/v1/assistant/sessions/'),
            ('post',   '/api/v1/assistant/ask/'),
            ('post',   '/api/v1/records/upload/text/'),
        ]:
            with self.subTest(url=url):
                resp = getattr(self.client, method)(url)
                self.assertIn(resp.status_code, (401, 403))


@override_settings(REST_FRAMEWORK=NO_THROTTLE)
class PrivilegeEscalationTests(APITestCase):
    """
    Self-service registration must always produce a patient.

    `role` was a writable field on RegisterSerializer while CustomUser.is_approved
    defaults to True, so a single unauthenticated POST could mint an approved
    hospital_admin — which can list every patient and link any doctor to any
    patient, and from there read their records.
    """

    def setUp(self):
        cache.clear()

    def _register(self, **extra):
        return self.client.post('/api/v1/auth/register/', {
            'email': 'newuser@example.com',
            'password': 'sufficiently-long-pw',
            'password2': 'sufficiently-long-pw',
            **extra,
        }, format='json')

    def test_registration_creates_a_patient(self):
        resp = self._register()
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(User.objects.get(email='newuser@example.com').role, 'patient')

    def test_cannot_self_register_as_privileged_role(self):
        for role in ('doctor', 'hospital_admin', 'data_scientist', 'admin'):
            with self.subTest(role=role):
                User.objects.filter(email='newuser@example.com').delete()
                resp = self._register(role=role)
                self.assertEqual(resp.status_code, 201)
                user = User.objects.get(email='newuser@example.com')
                self.assertEqual(
                    user.role, 'patient',
                    f'privilege escalation: self-registration granted role={user.role}',
                )

    def test_cannot_self_register_as_staff_or_superuser(self):
        resp = self._register(is_staff=True, is_superuser=True)
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email='newuser@example.com')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_profile_update_cannot_change_role(self):
        user = User.objects.create_user(
            username='pu', email='pu@example.com', password='pw-profile-1',
        )
        self.client.force_authenticate(user=user)
        self.client.patch('/api/v1/auth/profile/',
                          {'role': 'admin', 'is_staff': True}, format='json')
        user.refresh_from_db()
        self.assertEqual(user.role, 'patient')
        self.assertFalse(user.is_staff)


class ThrottleTests(APITestCase):
    """
    Abusive request volumes must be rejected with 429.

    Rates are patched into SimpleRateThrottle.THROTTLE_RATES rather than through
    override_settings: DRF binds THROTTLE_RATES as a class attribute at import
    time, so overriding REST_FRAMEWORK in settings has no effect on throttles
    that are already imported.
    """

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    @staticmethod
    def _rates(**overrides):
        return patch.dict(SimpleRateThrottle.THROTTLE_RATES, overrides)

    def _hammer(self, times, fn):
        return [fn().status_code for _ in range(times)]

    def test_login_is_throttled_by_ip(self):
        payload = {'email': 'nobody@example.com', 'password': 'wrong'}
        with self._rates(auth_burst='3/min', auth_sustained='100/hour'):
            statuses = self._hammer(
                5, lambda: self.client.post('/api/v1/auth/login/', payload, format='json'),
            )
        self.assertIn(429, statuses, f'login was never throttled: {statuses}')
        self.assertEqual(statuses[-1], 429)

    def test_throttled_response_includes_retry_after(self):
        payload = {'email': 'nobody@example.com', 'password': 'wrong'}
        with self._rates(auth_burst='2/min', auth_sustained='100/hour'):
            last = None
            for _ in range(4):
                last = self.client.post('/api/v1/auth/login/', payload, format='json')
        self.assertEqual(last.status_code, 429)
        self.assertIn('Retry-After', last.headers)

    def test_registration_is_throttled(self):
        with self._rates(register='2/hour'):
            statuses = []
            for i in range(4):
                statuses.append(self.client.post('/api/v1/auth/register/', {
                    'email': f'user{i}@example.com',
                    'password': 'sufficiently-long-pw',
                    'password2': 'sufficiently-long-pw',
                }, format='json').status_code)
        self.assertIn(429, statuses, f'registration was never throttled: {statuses}')

    def test_password_reset_is_throttled(self):
        with self._rates(password_reset='2/hour'):
            statuses = self._hammer(4, lambda: self.client.post(
                '/api/v1/auth/forgot-password/',
                {'email': 'nobody@example.com'}, format='json',
            ))
        self.assertIn(429, statuses, f'password reset was never throttled: {statuses}')

    def test_upload_is_throttled_per_user(self):
        user = User.objects.create_user(
            username='uploader', email='uploader@example.com', password='pw-upload-1',
        )
        self.client.force_authenticate(user=user)
        with self._rates(upload='2/hour'):
            statuses = self._hammer(4, lambda: self.client.post(
                '/api/v1/records/upload/text/', {'text': 'a clinical note'}, format='json',
            ))
        self.assertIn(429, statuses, f'upload was never throttled: {statuses}')

    def test_ai_endpoint_is_throttled_per_user(self):
        user = User.objects.create_user(
            username='asker', email='asker@example.com', password='pw-asker-1',
        )
        self.client.force_authenticate(user=user)
        with self._rates(ai='2/min', ai_daily='100/day'):
            statuses = self._hammer(4, lambda: self.client.post(
                '/api/v1/assistant/ask/', {'query': 'hi'}, format='json',
            ))
        self.assertIn(429, statuses, f'AI endpoint was never throttled: {statuses}')

    def test_ocr_endpoint_is_throttled_per_user(self):
        user = User.objects.create_user(
            username='scanner', email='scanner@example.com', password='pw-scan-1',
        )
        self.client.force_authenticate(user=user)
        with self._rates(ocr='2/hour'):
            statuses = self._hammer(4, lambda: self.client.post('/api/v1/records/upload/scan/'))
        self.assertIn(429, statuses, f'OCR endpoint was never throttled: {statuses}')

    def test_throttle_is_per_user_not_global(self):
        """One user exhausting their budget must not lock out everyone else."""
        noisy = User.objects.create_user(
            username='noisy', email='noisy@example.com', password='pw-noisy-1',
        )
        quiet = User.objects.create_user(
            username='quiet', email='quiet@example.com', password='pw-quiet-1',
        )
        with self._rates(ai='2/min', ai_daily='100/day'):
            self.client.force_authenticate(user=noisy)
            self._hammer(4, lambda: self.client.post(
                '/api/v1/assistant/ask/', {'query': 'hi'}, format='json',
            ))
            self.client.force_authenticate(user=quiet)
            resp = self.client.post('/api/v1/assistant/ask/', {'query': 'hi'}, format='json')
        self.assertNotEqual(resp.status_code, 429)

    def test_normal_usage_is_not_throttled(self):
        """Production rates must leave ordinary client behaviour untouched."""
        user = User.objects.create_user(
            username='normal', email='normal@example.com', password='pw-normal-1',
        )
        self.client.force_authenticate(user=user)
        statuses = self._hammer(15, lambda: self.client.get('/api/v1/records/'))
        self.assertNotIn(429, statuses)

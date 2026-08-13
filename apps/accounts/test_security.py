"""
Security regression tests for the server-rendered (non-API) surface.

Covers the web login/registration/password-reset rate limits and object-level
authorization on the web views, including the doctor-access path — which is the
only place in the product where one user is ever permitted to read another's
medical data, and therefore the one that most needs a regression test.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import PatientDoctorRelationship
from apps.medical_records.models import MedicalRecord

User = get_user_model()


# django-ratelimit uses a FIXED window, so a burst that straddles a window
# boundary is split across two buckets. To trip a limit of N deterministically
# the burst must exceed 2N — otherwise a boundary landing mid-loop can leave both
# buckets under the limit and the test fails for reasons unrelated to the code.
# (Observed exactly that: 15 requests against a 10/m limit split 5+10.)
# The fast hasher keeps the larger burst quick: the login path deliberately runs
# a full password hash per request as a user-enumeration timing defence.
@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class WebAuthRateLimitTests(TestCase):
    """Login, registration and password reset must be rate limited by IP."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_login_is_rate_limited(self):
        # django-ratelimit binds the rate at import time, so overriding the
        # setting here would have no effect; drive past the configured rate instead.
        statuses = [
            self.client.post(reverse('accounts:login'),
                             {'username': 'nobody@example.com', 'password': 'wrong'}).status_code
            for _ in range(25)          # > 2 x the 10/m limit
        ]
        self.assertIn(429, statuses, f'web login was never rate limited: {statuses}')

    def test_registration_is_rate_limited(self):
        # Deliberately invalid (mismatched passwords) so the request is rejected
        # by the form: the limiter runs before form handling, and this keeps the
        # test independent of the account-creation path.
        statuses = []
        for i in range(12):         # > 2 x the 5/h limit
            statuses.append(self.client.post(reverse('accounts:register'), {
                'username': f'newuser{i}',
                'email': f'newuser{i}@example.com',
                'password1': 'sufficiently-long-pw-1',
                'password2': 'does-not-match',
            }).status_code)
        self.assertIn(429, statuses, f'web registration was never rate limited: {statuses}')

    def test_password_reset_is_rate_limited(self):
        statuses = [
            self.client.post(reverse('accounts:password_reset'),
                             {'email': 'nobody@example.com'}).status_code
            for _ in range(12)          # > 2 x the 5/h limit
        ]
        self.assertIn(429, statuses, f'password reset was never rate limited: {statuses}')

    def test_a_single_valid_login_is_not_blocked(self):
        User.objects.create_user(
            username='realuser', email='real@example.com', password='pw-real-user-1',
        )
        resp = self.client.post(reverse('accounts:login'),
                                {'username': 'real@example.com', 'password': 'pw-real-user-1'})
        self.assertNotEqual(resp.status_code, 429)


class WebRecordIDORTests(TestCase):
    """Web record views must be scoped to the authenticated owner."""

    def setUp(self):
        cache.clear()
        self.attacker = User.objects.create_user(
            username='web-attacker', email='wa@example.com', password='pw-web-attack-1',
        )
        self.victim = User.objects.create_user(
            username='web-victim', email='wv@example.com', password='pw-web-victim-1',
        )
        self.record = MedicalRecord.objects.create(
            patient=self.victim, title='Victim MRI Report',
            record_type='imaging', raw_text='Lesion observed in left lobe',
        )
        self.client.force_login(self.attacker)

    def test_cannot_view_another_users_record(self):
        resp = self.client.get(f'/records/{self.record.pk}/')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('Victim MRI Report', resp.content.decode())

    def test_cannot_delete_another_users_record(self):
        self.client.post(f'/records/{self.record.pk}/delete/')
        self.assertTrue(MedicalRecord.objects.filter(pk=self.record.pk).exists())

    def test_record_list_excludes_other_users_records(self):
        resp = self.client.get('/records/')
        self.assertNotIn('Victim MRI Report', resp.content.decode())

    def test_owner_can_view_own_record(self):
        self.client.force_login(self.victim)
        resp = self.client.get(f'/records/{self.record.pk}/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Victim MRI Report', resp.content.decode())


class DoctorAccessAuthorizationTests(TestCase):
    """
    Doctor access is the one cross-user read path in the product.

    It must require BOTH the doctor role AND an active PatientDoctorRelationship.
    A doctor with no link is just another unauthorized user.
    """

    def setUp(self):
        cache.clear()
        self.patient = User.objects.create_user(
            username='dr-patient', email='dp@example.com', password='pw-patient-1',
            role='patient',
        )
        self.linked_doctor = User.objects.create_user(
            username='linked-doc', email='ld@example.com', password='pw-doc-1',
            role='doctor',
        )
        self.unlinked_doctor = User.objects.create_user(
            username='unlinked-doc', email='ud@example.com', password='pw-doc-2',
            role='doctor',
        )
        PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=self.linked_doctor,
            status=PatientDoctorRelationship.Status.ACTIVE,
        )
        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Confidential Diagnosis',
            record_type='diagnosis', raw_text='Stage II carcinoma',
        )

    def test_unlinked_doctor_cannot_view_patient_records(self):
        self.client.force_login(self.unlinked_doctor)
        resp = self.client.get(f'/dashboard/patient/{self.patient.pk}/records/')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('Confidential Diagnosis', resp.content.decode())

    def test_unlinked_doctor_cannot_view_a_specific_record(self):
        self.client.force_login(self.unlinked_doctor)
        resp = self.client.get(f'/dashboard/record/{self.record.pk}/')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('Stage II carcinoma', resp.content.decode())

    def test_patient_role_cannot_use_the_doctor_view_at_all(self):
        other_patient = User.objects.create_user(
            username='nosy', email='nosy@example.com', password='pw-nosy-1', role='patient',
        )
        self.client.force_login(other_patient)
        resp = self.client.get(f'/dashboard/patient/{self.patient.pk}/records/', follow=True)
        self.assertNotIn('Confidential Diagnosis', resp.content.decode())

    def test_deactivated_link_revokes_access(self):
        rel = PatientDoctorRelationship.objects.get(
            patient=self.patient, doctor=self.linked_doctor,
        )
        rel.status = PatientDoctorRelationship.Status.REVOKED
        rel.save(update_fields=['status'])

        self.client.force_login(self.linked_doctor)
        resp = self.client.get(f'/dashboard/patient/{self.patient.pk}/records/')
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('Confidential Diagnosis', resp.content.decode())

    def test_linked_doctor_can_view_and_access_is_logged(self):
        """The authorized path must keep working, and must leave an audit trail."""
        from apps.accounts.models import DoctorAccessLog

        self.client.force_login(self.linked_doctor)
        resp = self.client.get(f'/dashboard/patient/{self.patient.pk}/records/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Confidential Diagnosis', resp.content.decode())
        self.assertTrue(
            DoctorAccessLog.objects.filter(
                actor=self.linked_doctor, patient=self.patient,
            ).exists()
        )


class MediaOwnershipTests(TestCase):
    """The /media/ handler must enforce ownership and block traversal."""

    def setUp(self):
        cache.clear()
        self.attacker = User.objects.create_user(
            username='media-attacker', email='ma@example.com', password='pw-media-1',
        )

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get('/media/medical_records/2026/01/victim.pdf')
        self.assertIn(resp.status_code, (302, 404))

    def test_authenticated_user_cannot_fetch_unowned_file(self):
        self.client.force_login(self.attacker)
        resp = self.client.get('/media/medical_records/2026/01/victim.pdf')
        self.assertIn(resp.status_code, (403, 404))

    def test_path_traversal_is_rejected(self):
        self.client.force_login(self.attacker)
        resp = self.client.get('/media/../../healthcompass/settings.py')
        self.assertIn(resp.status_code, (403, 404))
        self.assertNotIn(b'SECRET_KEY', resp.content)


class WebRegistrationTests(TestCase):
    """
    Successful web registration must create the account, log the user in, and
    redirect to the dashboard.

    Regression: register_view called login(request, user) with three entries in
    AUTHENTICATION_BACKENDS and no backend on the user object, so Django raised
    ValueError and every successful signup returned a 500.
    """

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _payload(self, **over):
        data = {
            'username': 'brandnew',
            'first_name': 'Brand',
            'last_name': 'New',
            'email': 'brandnew@example.com',
            'password1': 'sufficiently-long-pw-1',
            'password2': 'sufficiently-long-pw-1',
        }
        data.update(over)
        return data

    def test_registration_succeeds_and_logs_the_user_in(self):
        resp = self.client.post(reverse('accounts:register'), self._payload())

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('dashboard:home'))
        self.assertTrue(User.objects.filter(username='brandnew').exists())
        self.assertIn('_auth_user_id', self.client.session)
        self.assertEqual(
            int(self.client.session['_auth_user_id']),
            User.objects.get(username='brandnew').pk,
        )

    def test_registration_records_the_authenticating_backend(self):
        """Proves the login went through the backend chain, not around it."""
        self.client.post(reverse('accounts:register'), self._payload())
        self.assertEqual(
            self.client.session['_auth_user_backend'],
            'apps.accounts.backends.EmailOrUsernameBackend',
        )

    def test_registration_creates_a_patient_profile(self):
        from apps.accounts.models import PatientProfile

        self.client.post(reverse('accounts:register'), self._payload())
        user = User.objects.get(username='brandnew')
        self.assertTrue(PatientProfile.objects.filter(user=user).exists())

    def test_web_registration_always_creates_a_patient(self):
        """The web form has no role field; posting one must not grant it."""
        self.client.post(reverse('accounts:register'),
                         self._payload(role='hospital_admin', is_staff='true'))
        user = User.objects.get(username='brandnew')
        self.assertEqual(user.role, 'patient')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_registered_user_can_log_in_afterwards(self):
        self.client.post(reverse('accounts:register'), self._payload())
        self.client.get(reverse('accounts:logout'))

        resp = self.client.post(reverse('accounts:login'), {
            'username': 'brandnew@example.com',
            'password': 'sufficiently-long-pw-1',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_invalid_registration_does_not_create_a_user(self):
        resp = self.client.post(reverse('accounts:register'),
                                self._payload(password2='does-not-match'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username='brandnew').exists())
        self.assertNotIn('_auth_user_id', self.client.session)

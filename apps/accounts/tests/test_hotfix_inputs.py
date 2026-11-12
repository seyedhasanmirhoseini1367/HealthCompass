"""Regression tests for audit CQ-03 (inputs that returned 500) and GET logout."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.forms import ProfileForm
from apps.accounts.models import PatientProfile
from apps.medical_records.models import MedicalRecord

User = get_user_model()


class _User(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            'inp_user', email='inp@test.invalid', password='pw-input-1', role='patient')
        self.api = APIClient()
        self.api.force_authenticate(self.user)


class BadDateFilterTests(_User):
    def test_web_records_ignores_a_malformed_date(self):
        self.client.force_login(self.user)
        response = self.client.get('/records/?period=custom&date_from=abc&date_to=2026-13-40')
        self.assertEqual(response.status_code, 200)

    def test_api_records_rejects_a_malformed_date_with_400(self):
        self.assertEqual(self.api.get('/api/v1/records/?date_from=abc').status_code, 400)

    def test_api_records_accepts_a_valid_date(self):
        self.assertEqual(self.api.get('/api/v1/records/?date_from=2026-01-01').status_code, 200)


class ApiProfileValidationTests(_User):
    def test_invalid_date_of_birth_is_400(self):
        response = self.api.patch('/api/v1/auth/profile/', {'date_of_birth': 'not-a-date'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_over_long_phone_is_400(self):
        response = self.api.patch('/api/v1/auth/profile/', {'phone_number': '1' * 50}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_valid_partial_update_succeeds(self):
        response = self.api.patch('/api/v1/auth/profile/', {'first_name': 'Aino'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Aino')


class ProfileEmailTests(_User):
    """The profile form no longer edits the email at all (prompt 2, SEC-05), so
    a case-variant duplicate can no longer reach the database through it."""

    def test_profile_form_has_no_email_field(self):
        self.assertNotIn('email', ProfileForm().fields)

    def test_posting_an_email_to_profile_edit_changes_nothing(self):
        User.objects.create_user('other', email='taken@test.invalid', password='pw-other-1')
        self.client.force_login(self.user)
        response = self.client.post('/accounts/profile/edit/', {'email': 'TAKEN@test.invalid'})
        self.assertLess(response.status_code, 500)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'inp@test.invalid')


class ApiEmergencyCardValidationTests(_User):
    def test_unknown_blood_type_is_400_and_not_saved(self):
        response = self.api.patch('/api/v1/auth/emergency-card/',
                                  {'blood_type': 'ZZZZZZZZZZ-not-a-type'}, format='json')
        self.assertEqual(response.status_code, 400)
        profile = PatientProfile.objects.get(user=self.user)
        self.assertEqual(profile.blood_type, '')

    def test_over_long_contact_name_is_400(self):
        response = self.api.patch('/api/v1/auth/emergency-card/',
                                  {'emergency_contact_name': 'x' * 500}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_valid_partial_update_keeps_other_fields(self):
        self.api.patch('/api/v1/auth/emergency-card/', {'allergies': 'Penicillin'}, format='json')
        response = self.api.patch('/api/v1/auth/emergency-card/', {'blood_type': 'O+'}, format='json')
        self.assertEqual(response.status_code, 200)
        profile = PatientProfile.objects.get(user=self.user)
        self.assertEqual((profile.blood_type, profile.allergies), ('O+', 'Penicillin'))


class UploadScanRecordTypeTests(_User):
    def test_unknown_record_type_is_stored_as_other(self):
        self.client.force_login(self.user)
        self.client.post('/records/upload/scan/', {'raw_text': 'Hb 140 g/L', 'record_type': 'bogus'})
        record = MedicalRecord.objects.filter(patient=self.user).first()
        self.assertIsNotNone(record)
        self.assertIn(record.record_type, MedicalRecord.RecordType.values)
        self.assertNotEqual(record.record_type, 'bogus')


class EmergencyCardLoginTests(TestCase):
    def test_anonymous_request_redirects_to_login(self):
        response = self.client.get('/accounts/emergency-card/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


class LogoutMethodTests(_User):
    def test_get_does_not_log_out(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/accounts/logout/').status_code, 405)
        self.assertIn('_auth_user_id', self.client.session)

    def test_post_logs_out(self):
        self.client.force_login(self.user)
        self.client.post('/accounts/logout/')
        self.assertNotIn('_auth_user_id', self.client.session)

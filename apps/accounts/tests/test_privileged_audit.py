"""
Tests for the privileged-account audit command and for the guarantee that
public registration can only ever create a patient.

The audit exists because the API once allowed `role` to be supplied at signup.
Those tests live here rather than in test_security.py so the audit tooling and
the invariant it protects stay next to each other.
"""
from io import StringIO
import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.accounts.models import DoctorProfile, HospitalAdminProfile

User = get_user_model()


def _run(*args):
    out = StringIO()
    call_command('audit_privileged_accounts', *args, stdout=out, stderr=StringIO())
    return out.getvalue()


class PrivilegedAccountAuditTests(TestCase):

    def setUp(self):
        cache.clear()
        # Legitimate: created administratively, has the matching profile and a
        # username distinct from the email.
        self.legit_doctor = User.objects.create_user(
            username='dr-house', email='house@hospital.example',
            password='pw-legit-doc-1', role='doctor',
        )
        DoctorProfile.objects.create(user=self.legit_doctor, specialty='Diagnostics')

        # Suspicious: the signature the vulnerable API path left behind —
        # username == email, and no DoctorProfile.
        self.api_doctor = User.objects.create_user(
            username='attacker@evil.example', email='attacker@evil.example',
            password='pw-api-doc-1', role='doctor',
        )

        # Suspicious: hospital_admin is the highest-value role in the chain.
        self.api_admin = User.objects.create_user(
            username='pwn@evil.example', email='pwn@evil.example',
            password='pw-api-admin-1', role='hospital_admin',
        )

        self.patient = User.objects.create_user(
            username='ordinary', email='ordinary@example.com',
            password='pw-ordinary-1', role='patient',
        )

    def test_report_lists_every_privileged_role(self):
        out = _run()
        self.assertIn('dr-house', out)
        self.assertIn('attacker@evil.example', out)
        self.assertIn('pwn@evil.example', out)

    def test_report_excludes_ordinary_patients(self):
        self.assertNotIn('ordinary', _run())

    def test_api_created_signature_is_flagged(self):
        out = _run()
        self.assertIn('username equals email (API-created signature)', out)

    def test_missing_role_profile_is_flagged(self):
        self.assertIn('no doctor_profile record', _run())
        self.assertIn('no hospital_admin_profile record', _run())

    def test_legitimate_doctor_is_not_flagged_for_profile_or_signature(self):
        rows = json.loads(_run('--json'))
        legit = next(r for r in rows if r['id'] == self.legit_doctor.pk)
        self.assertNotIn('no doctor_profile record', legit['flags'])
        self.assertNotIn('username equals email (API-created signature)', legit['flags'])

    def test_staff_patient_is_reported_despite_patient_role(self):
        self.patient.is_staff = True
        self.patient.save(update_fields=['is_staff'])
        rows = json.loads(_run('--json'))
        row = next(r for r in rows if r['id'] == self.patient.pk)
        self.assertIn('patient role but has staff/superuser privileges', row['flags'])

    def test_suspicious_only_filters_clean_accounts(self):
        rows = json.loads(_run('--json', '--suspicious-only'))
        self.assertTrue(all(r['flags'] for r in rows))
        self.assertIn(self.api_doctor.pk, [r['id'] for r in rows])

    def test_no_email_redacts_addresses(self):
        out = _run('--no-email')
        self.assertNotIn('house@hospital.example', out)
        self.assertIn('[redacted]', out)

    def test_json_output_carries_the_required_fields(self):
        rows = json.loads(_run('--json'))
        row = next(r for r in rows if r['id'] == self.api_doctor.pk)
        for field in ('id', 'username', 'email', 'role', 'is_approved',
                      'is_active', 'is_staff', 'date_joined', 'auth', 'flags'):
            self.assertIn(field, row)

    def test_auth_sources_are_reported(self):
        rows = json.loads(_run('--json'))
        row = next(r for r in rows if r['id'] == self.legit_doctor.pk)
        self.assertIn('password', row['auth'])

    def test_command_does_not_modify_any_account(self):
        """The audit is strictly read-only."""
        before = {
            u.pk: (u.role, u.is_approved, u.is_active, u.is_staff, u.is_superuser)
            for u in User.objects.all()
        }
        _run()
        after = {
            u.pk: (u.role, u.is_approved, u.is_active, u.is_staff, u.is_superuser)
            for u in User.objects.all()
        }
        self.assertEqual(before, after)
        self.assertEqual(User.objects.count(), len(before))

    def test_report_includes_revocation_guidance_when_flagged(self):
        out = _run()
        self.assertIn('Demote and suspend, do not delete', out)
        self.assertIn('DoctorAccessLog', out)

    def test_clean_database_reports_no_indicators(self):
        User.objects.exclude(pk=self.legit_doctor.pk).delete()
        self.legit_doctor.is_approved = False
        self.legit_doctor.save(update_fields=['is_approved'])
        out = _run()
        self.assertIn('No risk indicators found.', out)


class PublicRegistrationRoleInvariantTests(APITestCase):
    """
    The invariant the audit exists to protect: no public signup path — API or
    web — may create anything other than a patient.
    """

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_api_registration_ignores_supplied_role(self):
        for role in ('doctor', 'hospital_admin', 'data_scientist', 'admin'):
            with self.subTest(role=role):
                User.objects.filter(email='probe@example.com').delete()
                resp = self.client.post('/api/v1/auth/register/', {
                    'email': 'probe@example.com',
                    'password': 'sufficiently-long-pw',
                    'password2': 'sufficiently-long-pw',
                    'role': role,
                }, format='json')
                self.assertEqual(resp.status_code, 201)
                self.assertEqual(User.objects.get(email='probe@example.com').role, 'patient')

    def test_api_registration_ignores_privilege_flags(self):
        resp = self.client.post('/api/v1/auth/register/', {
            'email': 'probe2@example.com',
            'password': 'sufficiently-long-pw',
            'password2': 'sufficiently-long-pw',
            'is_staff': True, 'is_superuser': True, 'is_approved': True,
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email='probe2@example.com')
        self.assertEqual(user.role, 'patient')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_web_registration_ignores_supplied_role(self):
        self.client.post(reverse('accounts:register'), {
            'username': 'webprobe',
            'email': 'webprobe@example.com',
            'password1': 'sufficiently-long-pw-1',
            'password2': 'sufficiently-long-pw-1',
            'role': 'hospital_admin',
        })
        self.assertEqual(User.objects.get(username='webprobe').role, 'patient')

    def test_no_public_signup_creates_a_privileged_account(self):
        """Belt and braces: after exercising both paths, nothing is privileged."""
        self.client.post('/api/v1/auth/register/', {
            'email': 'a@example.com', 'password': 'sufficiently-long-pw',
            'password2': 'sufficiently-long-pw', 'role': 'doctor',
        }, format='json')
        self.client.post(reverse('accounts:register'), {
            'username': 'b', 'email': 'b@example.com',
            'password1': 'sufficiently-long-pw-1',
            'password2': 'sufficiently-long-pw-1', 'role': 'doctor',
        })
        self.assertFalse(
            User.objects.exclude(role='patient').exists(),
            'a public registration path created a privileged account',
        )

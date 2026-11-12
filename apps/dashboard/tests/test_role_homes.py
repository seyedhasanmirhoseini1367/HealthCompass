"""Each role's home, and the doctor access rule (prompt 9 task 2)."""
from django.test import TestCase

from apps.accounts.consent import grant_consent, revoke_consent
from apps.accounts.models import ConsentPurpose, DoctorAccessLog, PatientDoctorRelationship
from apps.testing.factories import make_doctor, make_patient


class RoleHomeTests(TestCase):
    def test_each_role_gets_a_home(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        for role in ('patient', 'doctor', 'data_scientist', 'hospital_admin'):
            with self.subTest(role=role):
                user = User.objects.create_user(f'home_{role}', email=f'h_{role}@test.invalid',
                                                password='pw', role=role)
                self.client.force_login(user)
                self.assertEqual(self.client.get('/dashboard/', follow=True).status_code, 200)


class DoctorAccessTests(TestCase):
    def setUp(self):
        self.patient, self.doctor = make_patient(), make_doctor()
        self.url = f'/dashboard/patient/{self.patient.pk}/records/'
        self.client.force_login(self.doctor)

    def _link(self, status):
        return PatientDoctorRelationship.objects.create(patient=self.patient, doctor=self.doctor, status=status)

    def test_no_link_is_404(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_pending_link_is_404(self):
        self._link(PatientDoctorRelationship.Status.PENDING)
        grant_consent(self.patient, ConsentPurpose.DATA_SHARING)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_active_link_without_consent_is_refused(self):
        self._link(PatientDoctorRelationship.Status.ACTIVE)
        grant_consent(self.patient, ConsentPurpose.DATA_SHARING)
        revoke_consent(self.patient, ConsentPurpose.DATA_SHARING)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_active_link_with_consent_works_and_is_logged(self):
        self._link(PatientDoctorRelationship.Status.ACTIVE)
        grant_consent(self.patient, ConsentPurpose.DATA_SHARING)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertTrue(DoctorAccessLog.objects.filter(actor=self.doctor, patient=self.patient).exists())

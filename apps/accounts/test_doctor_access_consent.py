"""
REGRESSION — NEW-05: who may read a patient's records, and who decides.

Three defects, all in the same flow:

1. **No hospital scope.** `create_link` accepted any patient id and any doctor
   id. Any hospital admin could link any doctor to any patient on the platform.
   `HospitalAdminProfile.hospital_name` existed and was never consulted.
2. **No patient consent.** The link was created `is_active=True`, so the doctor
   could read the records immediately; the patient was notified afterwards.
3. **No way to revoke.** `remove_link` is scoped `linked_by=request.user`, so
   only the admin who created a link could remove it — admin B could not undo
   admin A's link, and no patient-facing path existed at all. For a GDPR-scoped
   health product, a data subject who cannot terminate a third party's access to
   their own records is a compliance problem, not a UX gap.

`status` replaces the old `is_active` boolean rather than sitting beside it, so
there is one answer to "may this doctor read these records" rather than two
fields that can disagree.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import (
    DoctorAccessLog, DoctorProfile, HospitalAdminProfile,
    PatientDoctorRelationship,
)
from apps.medical_records.models import MedicalRecord

Status = PatientDoctorRelationship.Status
User = get_user_model()


class _Fixture(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            username='pat', password='pw-test-only', email='pat@example.com', role='patient')
        self.doctor = User.objects.create_user(
            username='doc', password='pw-test-only', email='doc@example.com', role='doctor')
        DoctorProfile.objects.create(user=self.doctor, hospital='Tampere University Hospital')

        self.admin = User.objects.create_user(
            username='hadmin', password='pw-test-only', email='ha@example.com',
            role='hospital_admin')
        HospitalAdminProfile.objects.create(
            user=self.admin, hospital_name='Tampere University Hospital')

        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')

    def _link(self, status=Status.PENDING):
        return PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=self.doctor,
            linked_by=self.admin, status=status)


class HospitalScopeTests(_Fixture):
    """An admin may only link doctors from their own hospital."""

    def test_admin_can_request_a_link_for_their_own_hospital(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('dashboard:create_link'),
                         {'patient_id': self.patient.pk, 'doctor_id': self.doctor.pk})
        self.assertTrue(PatientDoctorRelationship.objects.filter(
            patient=self.patient, doctor=self.doctor).exists())

    def test_admin_cannot_link_a_doctor_from_another_hospital(self):
        """ACCEPTANCE — NEW-05. Previously succeeded for any doctor."""
        other_doctor = User.objects.create_user(
            username='doc2', password='pw-test-only', email='d2@example.com', role='doctor')
        DoctorProfile.objects.create(user=other_doctor, hospital='Helsinki University Hospital')

        self.client.force_login(self.admin)
        self.client.post(reverse('dashboard:create_link'),
                         {'patient_id': self.patient.pk, 'doctor_id': other_doctor.pk})
        self.assertFalse(PatientDoctorRelationship.objects.filter(
            doctor=other_doctor).exists())

    def test_admin_with_no_hospital_cannot_link_anyone(self):
        """Fails closed: a blank affiliation must not match a blank one."""
        rogue = User.objects.create_user(
            username='rogue', password='pw-test-only', email='r@example.com',
            role='hospital_admin')
        HospitalAdminProfile.objects.create(user=rogue, hospital_name='')

        self.client.force_login(rogue)
        self.client.post(reverse('dashboard:create_link'),
                         {'patient_id': self.patient.pk, 'doctor_id': self.doctor.pk})
        self.assertFalse(PatientDoctorRelationship.objects.exists())


class ConsentGateTests(_Fixture):
    """A pending link grants nothing."""

    def test_new_links_start_pending(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('dashboard:create_link'),
                         {'patient_id': self.patient.pk, 'doctor_id': self.doctor.pk})
        link = PatientDoctorRelationship.objects.get(patient=self.patient)
        self.assertEqual(link.status, Status.PENDING)
        self.assertFalse(link.grants_access)

    def test_pending_link_does_not_expose_patient_records(self):
        """ACCEPTANCE — the doctor could read records the moment a link existed."""
        self._link(Status.PENDING)
        self.client.force_login(self.doctor)
        response = self.client.get(
            reverse('dashboard:patient_records', args=[self.patient.pk]))
        self.assertEqual(response.status_code, 404)

    def test_pending_link_does_not_expose_a_single_record(self):
        self._link(Status.PENDING)
        self.client.force_login(self.doctor)
        response = self.client.get(
            reverse('dashboard:doctor_record', args=[self.record.pk]))
        self.assertEqual(response.status_code, 404)

    def test_active_link_does_expose_records(self):
        """The guard must not break the legitimate flow."""
        self._link(Status.ACTIVE)
        self.client.force_login(self.doctor)
        response = self.client.get(
            reverse('dashboard:patient_records', args=[self.patient.pk]))
        self.assertEqual(response.status_code, 200)

    def test_revoked_link_stops_access_immediately(self):
        link = self._link(Status.ACTIVE)
        self.client.force_login(self.doctor)
        self.assertEqual(self.client.get(
            reverse('dashboard:patient_records', args=[self.patient.pk])).status_code, 200)

        link.status = Status.REVOKED
        link.save(update_fields=['status'])
        self.assertEqual(self.client.get(
            reverse('dashboard:patient_records', args=[self.patient.pk])).status_code, 404)


class PatientControlTests(_Fixture):
    """The capability that did not exist."""

    def test_patient_can_approve_a_request(self):
        link = self._link(Status.PENDING)
        self.client.force_login(self.patient)
        self.client.post(reverse('accounts:approve_doctor_access', args=[link.pk]))
        link.refresh_from_db()
        self.assertEqual(link.status, Status.ACTIVE)
        self.assertIsNotNone(link.decided_at)

    def test_patient_can_revoke_access(self):
        """ACCEPTANCE — NEW-05. No patient-facing revoke existed."""
        link = self._link(Status.ACTIVE)
        self.client.force_login(self.patient)
        self.client.post(reverse('accounts:revoke_doctor_access', args=[link.pk]))
        link.refresh_from_db()
        self.assertEqual(link.status, Status.REVOKED)

    def test_revocation_is_recorded_in_the_access_log(self):
        """The audit trail is what a patient can later ask to see."""
        link = self._link(Status.ACTIVE)
        self.client.force_login(self.patient)
        self.client.post(reverse('accounts:revoke_doctor_access', args=[link.pk]))
        self.assertTrue(DoctorAccessLog.objects.filter(
            patient=self.patient, resource__startswith='access_revoked').exists())

    def test_revoked_links_are_kept_not_deleted(self):
        """Who had access, and when it ended, is the history worth preserving."""
        link = self._link(Status.ACTIVE)
        self.client.force_login(self.patient)
        self.client.post(reverse('accounts:revoke_doctor_access', args=[link.pk]))
        self.assertTrue(PatientDoctorRelationship.objects.filter(pk=link.pk).exists())

    def test_a_patient_cannot_touch_another_patients_link(self):
        other = User.objects.create_user(
            username='pat2', password='pw-test-only', email='p2@example.com', role='patient')
        link = self._link(Status.PENDING)
        self.client.force_login(other)
        response = self.client.post(
            reverse('accounts:approve_doctor_access', args=[link.pk]))
        self.assertEqual(response.status_code, 404)
        link.refresh_from_db()
        self.assertEqual(link.status, Status.PENDING)

    def test_approve_and_revoke_are_post_only(self):
        """A GET must not change who can read medical records."""
        link = self._link(Status.PENDING)
        self.client.force_login(self.patient)
        for name in ('accounts:approve_doctor_access', 'accounts:revoke_doctor_access'):
            with self.subTest(view=name):
                self.assertEqual(
                    self.client.get(reverse(name, args=[link.pk])).status_code, 405)

    def test_my_doctors_page_lists_the_patients_own_links_only(self):
        self._link(Status.ACTIVE)
        other = User.objects.create_user(
            username='pat3', password='pw-test-only', email='p3@example.com', role='patient')
        PatientDoctorRelationship.objects.create(
            patient=other, doctor=self.doctor, status=Status.ACTIVE)

        self.client.force_login(self.patient)
        response = self.client.get(reverse('accounts:my_doctors'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['links']), 1)


class AdminRosterTests(_Fixture):
    """A hospital admin must not receive the platform-wide patient list."""

    def test_admin_dashboard_does_not_enumerate_all_patients(self):
        """ACCEPTANCE — this returned every patient on the platform, by name."""
        for i in range(3):
            User.objects.create_user(
                username=f'other{i}', password='pw-test-only',
                email=f'o{i}@example.com', role='patient')

        self.client.force_login(self.admin)
        response = self.client.get(reverse('dashboard:home'))
        self.assertEqual(len(response.context['all_patients']), 0)

    def test_admin_can_find_one_patient_by_exact_email(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('dashboard:home'),
                                   {'patient_email': 'pat@example.com'})
        self.assertEqual(len(response.context['all_patients']), 1)

    def test_partial_email_does_not_enumerate(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('dashboard:home'), {'patient_email': 'example.com'})
        self.assertEqual(len(response.context['all_patients']), 0)

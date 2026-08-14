"""
REGRESSION — SEC-1, SEC-2, SEC-3, SEC-6: media access, cohort data, audit trail.

SEC-1 · Any staff account could download any patient file, unlogged
-------------------------------------------------------------------
`_user_can_access_media` began with `if user.is_staff: return True`. Every
support account, every account created for a one-off admin task, could fetch any
patient's lab PDF, and nothing recorded that it happened. The patient asking
"who has read my records?" — which Finnish practice entitles them to ask — would
be told nobody had.

Staff access is still permitted (those accounts can read the same content
through the Django admin, so refusing here would be theatre), but it is now
written to the access trail.

SEC-6 · Linked doctors could NOT download their patient's files
----------------------------------------------------------------
The same function had no doctor branch at all. A doctor with an ACTIVE,
patient-approved link could open the record page and read its parsed lab values,
but downloading the source PDF returned 403. The gap pushes clinicians toward
side channels — email, screenshots — that leave the system entirely.

SEC-2 · Population analytics were open to every authenticated user
-------------------------------------------------------------------
`population_view` and the API's `population_insights` were authentication-only,
so any patient could read biomarker averages, alert counts and risk buckets
computed over every other patient.

SEC-3 · Deleting an account erased who performed an access
-----------------------------------------------------------
`DoctorAccessLog.actor` is SET_NULL, which correctly keeps the row when an
account is deleted — but the row then said only that *someone* accessed the
data. `actor_label` captures the identity at access time.
"""
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.authz import can_access_media, can_view_population_analytics
from apps.accounts.models import DoctorAccessLog, PatientDoctorRelationship
from apps.medical_records.models import MedicalRecord

User = get_user_model()
Status = PatientDoctorRelationship.Status


class MediaAccessTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'pat_media', email='pat_media@test.invalid', password='pw', role='patient')
        self.other = User.objects.create_user(
            'other_pat', email='other_pat@test.invalid', password='pw', role='patient')
        self.doctor = User.objects.create_user(
            'doc_media', email='doc_media@test.invalid', password='pw', role='doctor')
        self.staff = User.objects.create_user(
            'staff_media', email='staff_media@test.invalid', password='pw', is_staff=True)

        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Blood panel', record_type='lab_result')
        self.record.file.save('panel.pdf', ContentFile(b'%PDF-1.4 x'), save=True)
        self.path = self.record.file.name

    def _link(self, status):
        # Access needs an ACTIVE link AND the patient's DATA_SHARING consent.
        # These tests vary the status, so consent is granted alongside to keep
        # that the only moving part; the consent half has its own file.
        from apps.accounts.consent import grant_consent
        from apps.accounts.models import ConsentPurpose
        grant_consent(self.patient, ConsentPurpose.DATA_SHARING)

        return PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=self.doctor, status=status)

    # ── ownership ────────────────────────────────────────────────────────────

    def test_the_owner_can_read_their_own_file(self):
        self.assertTrue(can_access_media(self.patient, self.path))

    def test_reading_your_own_file_is_not_an_access_event(self):
        can_access_media(self.patient, self.path)
        self.assertEqual(DoctorAccessLog.objects.count(), 0)

    def test_another_patient_cannot_read_it(self):
        self.assertFalse(can_access_media(self.other, self.path))

    def test_a_refused_read_is_not_logged_as_an_access(self):
        can_access_media(self.other, self.path)
        self.assertEqual(DoctorAccessLog.objects.count(), 0)

    # ── SEC-1 · staff ────────────────────────────────────────────────────────

    def test_staff_access_is_recorded(self):
        """ACCEPTANCE — SEC-1. This read left no trace at all."""
        self.assertTrue(can_access_media(self.staff, self.path))

        entry = DoctorAccessLog.objects.get()
        self.assertEqual(entry.actor, self.staff)
        self.assertEqual(entry.patient, self.patient)
        self.assertIn(str(self.record.pk), entry.resource)

    def test_superuser_access_is_recorded_too(self):
        root = User.objects.create_superuser(
            'root_media', email='root_media@test.invalid', password='pw')
        self.assertTrue(can_access_media(root, self.path))
        self.assertEqual(DoctorAccessLog.objects.filter(actor=root).count(), 1)

    # ── SEC-6 · linked doctors ───────────────────────────────────────────────

    def test_a_linked_doctor_can_download_the_file(self):
        """ACCEPTANCE — SEC-6. The doctor could read the values but not the source."""
        self._link(Status.ACTIVE)
        self.assertTrue(can_access_media(self.doctor, self.path))

    def test_a_linked_doctors_download_is_recorded(self):
        self._link(Status.ACTIVE)
        can_access_media(self.doctor, self.path)
        self.assertEqual(DoctorAccessLog.objects.filter(actor=self.doctor).count(), 1)

    def test_a_pending_link_grants_nothing(self):
        self._link(Status.PENDING)
        self.assertFalse(can_access_media(self.doctor, self.path))

    def test_a_revoked_link_grants_nothing(self):
        self._link(Status.REVOKED)
        self.assertFalse(can_access_media(self.doctor, self.path))

    def test_an_unlinked_doctor_gets_nothing(self):
        self.assertFalse(can_access_media(self.doctor, self.path))

    def test_a_doctor_linked_to_someone_else_gets_nothing(self):
        PatientDoctorRelationship.objects.create(
            patient=self.other, doctor=self.doctor, status=Status.ACTIVE)
        self.assertFalse(can_access_media(self.doctor, self.path))

    # ── unattributable paths ─────────────────────────────────────────────────

    def test_an_unknown_path_is_refused_even_for_staff(self):
        """
        If we cannot say whose data a file is, we cannot say who may read it.
        The old code answered this with the staff bypass.
        """
        self.assertFalse(can_access_media(self.staff, 'medical_records/2020/01/ghost.pdf'))

    def test_a_model_artifact_is_not_logged_as_patient_access(self):
        """
        An ONNX file belongs to the data scientist who uploaded it. It is not
        anyone's health data, so reading it does not belong in the trail a
        patient can ask to see.
        """
        from django.core.files.base import ContentFile as _CF

        from apps.ai_insights.models import AIModel
        scientist = User.objects.create_user(
            'ds_media', email='ds_media@test.invalid', password='pw',
            role='data_scientist')
        model = AIModel.objects.create(
            data_scientist=scientist, name='Net', description='d')
        model.model_file.save('net.onnx', _CF(b'fake-onnx'), save=True)

        self.assertTrue(can_access_media(self.staff, model.model_file.name))
        self.assertEqual(DoctorAccessLog.objects.count(), 0)

    def test_a_profile_picture_belongs_to_its_owner(self):
        self.patient.profile_picture.save(
            'me.png', ContentFile(b'\x89PNG\r\n\x1a\n'), save=True)
        path = self.patient.profile_picture.name
        self.assertTrue(can_access_media(self.patient, path))
        self.assertFalse(can_access_media(self.other, path))


class MediaViewTests(TestCase):
    """The rule must actually be enforced at the HTTP boundary."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'pat_http', email='pat_http@test.invalid', password='pw', role='patient')
        self.intruder = User.objects.create_user(
            'nosy', email='nosy@test.invalid', password='pw', role='patient')
        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')
        self.record.file.save('http.pdf', ContentFile(b'%PDF-1.4 y'), save=True)
        self.url = f'/media/{self.record.file.name}'

    def test_anonymous_users_are_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (302, 301))

    def test_the_owner_gets_the_file(self):
        self.client.force_login(self.patient)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_another_patient_is_refused(self):
        self.client.force_login(self.intruder)
        self.assertEqual(self.client.get(self.url).status_code, 403)


class PopulationAnalyticsTests(TestCase):
    """SEC-2 — cohort statistics are not patient-facing."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'pat_pop', email='pat_pop@test.invalid', password='pw', role='patient')
        self.doctor = User.objects.create_user(
            'doc_pop', email='doc_pop@test.invalid', password='pw', role='doctor')
        self.scientist = User.objects.create_user(
            'ds_pop', email='ds_pop@test.invalid', password='pw', role='data_scientist')
        self.admin = User.objects.create_user(
            'ha_pop', email='ha_pop@test.invalid', password='pw', role='hospital_admin')
        self.staff = User.objects.create_user(
            'staff_pop', email='staff_pop@test.invalid', password='pw', is_staff=True)

    def test_a_patient_is_refused(self):
        """ACCEPTANCE — SEC-2."""
        self.assertFalse(can_view_population_analytics(self.patient))

    def test_a_doctor_is_refused(self):
        """A doctor's access is to their own patients, not to the cohort."""
        self.assertFalse(can_view_population_analytics(self.doctor))

    def test_research_and_admin_roles_are_allowed(self):
        for user in (self.scientist, self.admin, self.staff):
            with self.subTest(user=user.username):
                self.assertTrue(can_view_population_analytics(user))

    def test_anonymous_is_refused(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(can_view_population_analytics(AnonymousUser()))

    def test_the_web_view_redirects_a_patient(self):
        self.client.force_login(self.patient)
        response = self.client.get(reverse('ai_insights:population'))
        self.assertEqual(response.status_code, 302)

    def test_the_web_view_serves_a_data_scientist(self):
        self.client.force_login(self.scientist)
        response = self.client.get(reverse('ai_insights:population'))
        self.assertEqual(response.status_code, 200)

    def _api(self, user):
        """The API authenticates with JWT, so a session login is not enough."""
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_the_api_refuses_a_patient(self):
        response = self._api(self.patient).get('/api/v1/population/')
        self.assertEqual(response.status_code, 403)

    def test_the_api_serves_a_data_scientist(self):
        response = self._api(self.scientist).get('/api/v1/population/')
        self.assertEqual(response.status_code, 200)

    def test_the_api_check_precedes_the_cache(self):
        """A cached payload must not become a way around the check."""
        self.assertEqual(self._api(self.scientist).get('/api/v1/population/').status_code, 200)
        self.assertEqual(self._api(self.patient).get('/api/v1/population/').status_code, 403)


class AccessLogDurabilityTests(TestCase):
    """SEC-3 — the trail must outlive the accounts named in it."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'pat_log', email='pat_log@test.invalid', password='pw', role='patient')
        self.doctor = User.objects.create_user(
            'doc_log', email='doc_log@test.invalid', password='pw', role='doctor')

    def test_the_actor_label_is_captured_on_write(self):
        entry = DoctorAccessLog.objects.create(
            actor=self.doctor, patient=self.patient, resource='patient_records')
        self.assertIn('doc_log', entry.actor_label)
        self.assertIn('doctor', entry.actor_label)

    def test_the_identity_survives_deleting_the_actor(self):
        """ACCEPTANCE — SEC-3. The row used to be reduced to 'someone'."""
        DoctorAccessLog.objects.create(
            actor=self.doctor, patient=self.patient, resource='patient_records')
        self.doctor.delete()

        entry = DoctorAccessLog.objects.get()
        self.assertIsNone(entry.actor)
        self.assertIn('doc_log', entry.actor_label)

    def test_the_row_itself_survives(self):
        DoctorAccessLog.objects.create(
            actor=self.doctor, patient=self.patient, resource='patient_records')
        self.doctor.delete()
        self.assertEqual(DoctorAccessLog.objects.count(), 1)

    def test_the_label_is_not_rewritten_on_later_saves(self):
        """An audit row records what was true then, not what is true now."""
        entry = DoctorAccessLog.objects.create(
            actor=self.doctor, patient=self.patient, resource='patient_records')
        original = entry.actor_label

        self.doctor.username = 'renamed_doctor'
        self.doctor.save(update_fields=['username'])
        entry.save()
        entry.refresh_from_db()

        self.assertEqual(entry.actor_label, original)

    def test_the_patient_side_is_still_anonymised_by_erasure(self):
        """
        Erasure anonymises the patient reference on purpose. Denormalising the
        patient's identity here would have defeated it.
        """
        DoctorAccessLog.objects.create(
            actor=self.doctor, patient=self.patient, resource='patient_records')

        from apps.accounts.services import purge_user_data
        purge_user_data(self.patient)

        entry = DoctorAccessLog.objects.get()
        self.assertIsNone(entry.patient)
        for field in (entry.resource, entry.actor_label):
            self.assertNotIn('pat_log', field)

    def test_the_export_names_the_actor_even_after_deletion(self):
        DoctorAccessLog.objects.create(
            actor=self.doctor, patient=self.patient, resource='patient_records')
        self.doctor.delete()

        from apps.accounts.export import _access_history
        payload = _access_history(self.patient)
        entry = payload['clinician_access_to_my_records'][0]
        self.assertIn('doc_log', entry['accessed_by'])

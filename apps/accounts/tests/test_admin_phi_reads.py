"""
F4 (first half) — privileged reads of patient data are now recorded.

Every control this system has built — consent, doctor_has_active_link,
log_phi_access — is bypassed by the Django admin, which reaches the ORM
directly. Opening a patient's record there left no trace, while that patient's
data export presents "who accessed my records" as a complete answer. An
incomplete answer offered as a complete one is worse than none.

Deliberately NOT done here: restricting the admin. Lab values are written by
ingestion and two seed commands, and no view, form, endpoint or management
command edits them — so the admin is the only way to correct a mis-parsed
clinical value, and a wrong unit can raise a false critical alert. Removing or
freezing it needs a correction workflow behind it, which is a decision about
clinical provenance rather than something to settle in passing.

This is the half that costs nothing: the same access, now visible.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import DoctorAccessLog
from apps.ai_insights.models import AIModel, HealthAlert, ModelPrediction
from apps.medical_records.models import MedicalRecord
from apps.rag_assistant.models import ChatSession, QueryLog

User = get_user_model()


class _AdminReads(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(
            'phi_admin', email='phi_admin@test.invalid', password='pw-admin-1')
        self.patient = User.objects.create_user(
            'phi_patient', email='phi_patient@test.invalid', password='pw', role='patient')
        self.client.force_login(self.admin)
        DoctorAccessLog.objects.all().delete()

    def _open(self, url_name, pk):
        return self.client.get(reverse(url_name, args=[pk]))


class RecordReadsAreLoggedTests(_AdminReads):

    def test_opening_a_medical_record_is_recorded(self):
        """ACCEPTANCE — F4. This left no trace anywhere."""
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')

        self._open('admin:medical_records_medicalrecord_change', record.pk)

        entry = DoctorAccessLog.objects.get()
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.patient, self.patient)
        self.assertIn(str(record.pk), entry.resource)
        self.assertIn('admin', entry.resource)

    def test_the_admin_can_still_open_and_correct_the_record(self):
        """Logging must not cost the only clinical correction path."""
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')

        response = self._open('admin:medical_records_medicalrecord_change', record.pk)
        self.assertEqual(response.status_code, 200)

    def test_the_actor_label_survives_for_the_patients_trail(self):
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')
        self._open('admin:medical_records_medicalrecord_change', record.pk)

        self.assertIn('phi_admin', DoctorAccessLog.objects.get().actor_label)

    def test_listing_records_is_not_logged_as_an_access(self):
        """
        Paging a changelist is not reading a patient. Logging every row would
        bury the events that matter in noise nobody reads.
        """
        MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')

        self.client.get(reverse('admin:medical_records_medicalrecord_changelist'))
        self.assertEqual(DoctorAccessLog.objects.count(), 0)

    def test_a_missing_object_records_nothing(self):
        self._open('admin:medical_records_medicalrecord_change',
                   '00000000-0000-0000-0000-000000000009')
        self.assertEqual(DoctorAccessLog.objects.count(), 0)


class EveryPhiModelIsCoveredTests(_AdminReads):

    def test_opening_a_health_alert_is_recorded(self):
        alert = HealthAlert.objects.create(
            patient=self.patient, title='High glucose', message='m', severity='warning')
        self._open('admin:ai_insights_healthalert_change', alert.pk)
        self.assertEqual(DoctorAccessLog.objects.count(), 1)

    def test_opening_a_prediction_is_recorded(self):
        model = AIModel.objects.create(
            data_scientist=None, is_system=True, name='S', description='d')
        prediction = ModelPrediction.objects.create(model=model, patient=self.patient)

        self._open('admin:ai_insights_modelprediction_change', prediction.pk)
        self.assertEqual(DoctorAccessLog.objects.count(), 1)

    def test_opening_a_chat_session_is_recorded(self):
        session = ChatSession.objects.create(patient=self.patient, title='Chat')
        self._open('admin:rag_assistant_chatsession_change', session.pk)
        self.assertEqual(DoctorAccessLog.objects.count(), 1)

    def test_opening_a_query_log_is_recorded_against_the_sessions_patient(self):
        """
        A QueryLog has no patient of its own. Reading one exposes the patient's
        question and the assistant's answer about their records.
        """
        session = ChatSession.objects.create(patient=self.patient, title='Chat')
        log = QueryLog.objects.create(session=session, query='q', response='r')

        self._open('admin:rag_assistant_querylog_change', log.pk)

        entry = DoctorAccessLog.objects.get()
        self.assertEqual(entry.patient, self.patient)

    def test_the_resource_names_the_model_but_not_its_content(self):
        """
        The trail says what was opened, never what it said — otherwise reading
        the trail becomes a second way to read records.
        """
        session = ChatSession.objects.create(patient=self.patient, title='Chat')
        log = QueryLog.objects.create(
            session=session, query='Do I have cancer?', response='Your results...')

        self._open('admin:rag_assistant_querylog_change', log.pk)

        resource = DoctorAccessLog.objects.get().resource
        self.assertNotIn('cancer', resource)
        self.assertIn('querylog', resource)


class SelfReadsAreNotLoggedTests(_AdminReads):

    def test_reading_your_own_row_is_not_an_access_event(self):
        """Consistent with can_access_media: the subject already knows."""
        record = MedicalRecord.objects.create(
            patient=self.admin, title='Own', record_type='lab_result')

        self._open('admin:medical_records_medicalrecord_change', record.pk)
        self.assertEqual(DoctorAccessLog.objects.count(), 0)


class LoggingFailureDoesNotBreakTheReadTests(_AdminReads):

    def test_a_failure_to_log_still_serves_the_page(self):
        from unittest.mock import patch

        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')

        with patch('apps.accounts.models.DoctorAccessLog.objects.create',
                   side_effect=RuntimeError('table gone')):
            with self.assertLogs('healthcompass.ops', level='ERROR'):
                response = self._open(
                    'admin:medical_records_medicalrecord_change', record.pk)

        self.assertEqual(response.status_code, 200)

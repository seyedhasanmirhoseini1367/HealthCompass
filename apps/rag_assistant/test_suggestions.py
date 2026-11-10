"""Starter questions come from the user's own data."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.medical_records.models import MedicalRecord, ParsedLabValue
from apps.rag_assistant.suggestions import GENERAL, PROFESSIONAL, starter_questions

User = get_user_model()


class StarterQuestionTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'sq_patient', email='sq@test.invalid', password='pw', role='patient')

    def _record(self):
        return MedicalRecord.objects.create(patient=self.patient, title='Panel',
                                            record_type='lab_result')

    def _lab(self, record, name, abnormal=False):
        return ParsedLabValue.objects.create(record=record, parameter_name=name, value='5',
                                             is_abnormal=abnormal, measured_at=timezone.now())

    def test_no_records_gets_general_questions_only(self):
        qs = starter_questions(self.patient)
        self.assertEqual(qs, GENERAL[:4])

    def test_lab_values_name_the_users_own_analytes(self):
        r = self._record()
        self._lab(r, 'Ferritin', abnormal=True)
        self._lab(r, 'Glucose')
        self._lab(self._record(), 'Glucose')
        qs = starter_questions(self.patient)
        self.assertIn('What do my latest lab results show?', qs)
        self.assertIn('What does my Ferritin result mean?', qs)
        self.assertIn('How has my Glucose changed over time?', qs)
        self.assertLessEqual(len(qs), 4)
        self.assertEqual(len(qs), len(set(qs)))

    def test_another_patients_labs_are_never_used(self):
        other = User.objects.create_user('sq_other', email='sqo@test.invalid',
                                         password='pw', role='patient')
        rec = MedicalRecord.objects.create(patient=other, title='X', record_type='lab_result')
        ParsedLabValue.objects.create(record=rec, parameter_name='Secretin', value='1',
                                      is_abnormal=True)
        self.assertFalse(any('Secretin' in q for q in starter_questions(self.patient)))

    def test_professionals_get_clinical_questions(self):
        doctor = User.objects.create_user('sq_doc', email='sqd@test.invalid',
                                          password='pw', role='doctor')
        self.assertEqual(starter_questions(doctor), PROFESSIONAL[:4])

    def test_empty_chat_renders_them(self):
        self.client.force_login(self.patient)
        r = self.client.get('/assistant/')
        self.assertContains(r, GENERAL[0])

"""
Medications and conditions must not escape through the side channels.

The access tests cover the front door. This covers the places PHI leaks without
anyone deciding to disclose it: a log line, an admin listing, an exception
message shown to a user. A drug name or a diagnosis in a log is read by
operators who have no business seeing either, and unlike a page it carries no
authorization at all.
"""
import logging
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.medical_records.models import (ConditionStatement, MedicalRecord,
                                         MedicationStatement)
from apps.medical_records.services import _save_clinical_state

User = get_user_model()

#: Distinctive enough that finding it anywhere is unambiguous.
DRUG = 'Zidovudine'
DIAGNOSIS = 'HIV infection'


class LoggingTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'csd', email='csd@test.invalid', password='pw', role='patient')
        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Note', record_type='other',
            record_date=date(2026, 1, 1))

    def test_a_failed_medication_write_does_not_log_the_drug(self):
        """ACCEPTANCE — a database error can carry the value back in its text."""
        from unittest.mock import patch

        with patch.object(MedicationStatement.objects, 'create',
                          side_effect=RuntimeError(f'bad value: {DRUG}')):
            with self.assertLogs('apps.medical_records.services', level='WARNING') as logs:
                _save_clinical_state(self.record, {'medications': [{'name': DRUG}]})

        self.assertNotIn(DRUG, '\n'.join(logs.output))

    def test_a_failed_condition_write_does_not_log_the_diagnosis(self):
        from unittest.mock import patch

        with patch.object(ConditionStatement.objects, 'create',
                          side_effect=RuntimeError(f'bad value: {DIAGNOSIS}')):
            with self.assertLogs('apps.medical_records.services', level='WARNING') as logs:
                _save_clinical_state(
                    self.record, {'diagnoses': [{'description': DIAGNOSIS}]})

        self.assertNotIn(DIAGNOSIS, '\n'.join(logs.output))

    def test_the_failure_is_still_reported(self):
        """Not logging the value is not the same as saying nothing."""
        from unittest.mock import patch

        with patch.object(MedicationStatement.objects, 'create',
                          side_effect=RuntimeError('boom')):
            with self.assertLogs('apps.medical_records.services', level='WARNING') as logs:
                created = _save_clinical_state(
                    self.record, {'medications': [{'name': DRUG}]})

        self.assertEqual(created, 0)
        self.assertIn(str(self.record.pk), '\n'.join(logs.output))
        self.assertIn('RuntimeError', '\n'.join(logs.output))

    def test_a_malformed_block_does_not_lose_the_record(self):
        """The document is the primary artifact; extraction is secondary."""
        _save_clinical_state(self.record, {'medications': 'not a list at all'})

        self.assertTrue(MedicalRecord.objects.filter(pk=self.record.pk).exists())


class AdminExposureTests(TestCase):
    """
    Neither statement model is registered in the admin.

    Deliberate. The admin is where PHI is most easily browsed in bulk and the
    least protected by this application's own predicates, and nothing needs a
    hand-edit path here: statements are evidence of what a document said, so
    correcting one means correcting the document, not the row. Registering them
    would be new exposure buying nothing.
    """

    def test_the_statement_models_are_not_in_the_admin(self):
        from django.contrib import admin

        self.assertNotIn(MedicationStatement, admin.site._registry)
        self.assertNotIn(ConditionStatement, admin.site._registry)


class RepresentationTests(TestCase):
    """
    `__str__` DOES contain the drug name, and that is correct.

    It is what makes a row identifiable to someone already entitled to see it.
    The rule is not "never put PHI in __str__" — it is "never send __str__
    somewhere unauthenticated", which is why the logging tests above matter and
    why these models stay out of the admin.
    """

    def setUp(self):
        self.patient = User.objects.create_user(
            'csd2', email='csd2@test.invalid', password='pw', role='patient')
        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Note', record_type='other',
            record_date=date(2026, 1, 1))

    def test_a_statement_identifies_itself_with_its_date_and_status(self):
        statement = MedicationStatement.objects.create(
            record=self.record, patient=self.patient, name=DRUG)

        text = str(statement)
        self.assertIn(DRUG, text)
        self.assertIn('2026-01-01', text)

    def test_a_code_only_condition_still_has_a_readable_label(self):
        statement = ConditionStatement.objects.create(
            record=self.record, patient=self.patient, code='E11')

        self.assertIn('E11', str(statement))

    def test_a_condition_with_nothing_at_all_does_not_render_blank(self):
        statement = ConditionStatement(record=self.record, patient=self.patient)
        self.assertIn('unnamed', str(statement))

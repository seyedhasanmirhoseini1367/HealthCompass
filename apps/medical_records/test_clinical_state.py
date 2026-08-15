"""
Medication and condition state — resolved from what documents asserted.

The gap this closes
-------------------
The ingestion parser extracted `medications` and `diagnoses` into
`parsed_data` and a repo-wide search found **zero** consumers. A discharge
summary listing six drugs produced six entries nobody could query, and the
assistant could not answer "what am I taking" from structured data at all.

Why statements rather than a flag
---------------------------------
"Patient is on metformin" as a boolean loses when it started, when it stopped,
and which document said so. A patient asking "am I still taking this?" and a
clinician asking "when was this discontinued?" are the same query at different
points on a timeline, and only the history answers both.

So the same discipline as the lab values: the assertion is immutable evidence of
what a document said, a later document supersedes rather than overwrites, and
two documents disagreeing on one date is reported rather than resolved by
picking.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.medical_records import clinical_state as state
from apps.medical_records.models import (ConditionStatement, MedicalRecord,
                                         MedicationStatement)

User = get_user_model()


class _State(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'st_patient', email='st_patient@test.invalid', password='pw', role='patient')

    def _record(self, when=date(2026, 1, 1), title='Discharge summary'):
        return MedicalRecord.objects.create(
            patient=self.patient, title=title, record_type='discharge',
            record_date=when)

    def _med(self, name='Metformin', status=MedicationStatement.Status.ACTIVE,
             when=date(2026, 1, 1), dose='500mg', record=None):
        return MedicationStatement.objects.create(
            record=record or self._record(when), patient=self.patient,
            name=name, dose=dose, status=status)

    def _cond(self, description='Type 2 diabetes',
              status=ConditionStatement.Status.ACTIVE, when=date(2026, 1, 1)):
        return ConditionStatement.objects.create(
            record=self._record(when), patient=self.patient,
            description=description, status=status)


class MedicationResolutionTests(_State):

    def test_a_single_assertion_is_current(self):
        self._med()
        current = state.current_medications(self.patient)

        self.assertEqual(len(current), 1)
        self.assertEqual(current[0].statement.name, 'Metformin')

    def test_a_later_document_supersedes_an_earlier_one(self):
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 1, 1))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 6, 1))

        self.assertEqual(state.current_medications(self.patient), [])
        self.assertEqual(len(state.discontinued_medications(self.patient)), 1)

    def test_restarting_a_medication_makes_it_current_again(self):
        """A timeline, not a one-way door."""
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 1, 1))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 3, 1))
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 6, 1))

        self.assertEqual(len(state.current_medications(self.patient)), 1)

    def test_the_earlier_assertion_is_never_overwritten(self):
        """Both documents still say what they said."""
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 1, 1))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 6, 1))

        history = state.medication_history(self.patient, 'Metformin')
        self.assertEqual([s.status for s in history], ['active', 'discontinued'])

    def test_different_medications_resolve_independently(self):
        self._med(name='Metformin')
        self._med(name='Ramipril', status=MedicationStatement.Status.DISCONTINUED)

        current = [r.statement.name for r in state.current_medications(self.patient)]
        self.assertEqual(current, ['Metformin'])

    def test_matching_ignores_case_and_spacing_only(self):
        """
        Deliberately not a drug vocabulary: deciding that "Metformin" and
        "Metformin HCl 500mg" are one medication is a clinical judgement, and
        getting it wrong merges two prescriptions or splits one history.
        """
        self._med(name='Metformin', when=date(2026, 1, 1))
        self._med(name='  metformin  ', status=MedicationStatement.Status.DISCONTINUED,
                  when=date(2026, 6, 1))

        self.assertEqual(state.current_medications(self.patient), [])

        self._med(name='Metformin HCl', when=date(2026, 7, 1))
        self.assertEqual(len(state.current_medications(self.patient)), 1)

    def test_history_is_oldest_first(self):
        self._med(when=date(2026, 1, 1))
        self._med(when=date(2026, 6, 1))

        history = state.medication_history(self.patient, 'Metformin')
        self.assertEqual([s.asserted_on for s in history],
                         [date(2026, 1, 1), date(2026, 6, 1)])

    def test_an_unknown_medication_has_no_history(self):
        self.assertEqual(state.medication_history(self.patient, 'Aspirin'), [])


class ConflictTests(_State):
    """Two documents, one date, opposite claims. The layer must not pick."""

    def test_same_date_disagreement_is_reported(self):
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 5, 5))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 5, 5))

        conflicted = state.conflicted_medications(self.patient)
        self.assertEqual(len(conflicted), 1)
        self.assertEqual(len(conflicted[0].conflicting), 2)

    def test_a_conflicted_medication_is_not_reported_as_current(self):
        """ACCEPTANCE — saying "yes you are taking it" would pick a side."""
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 5, 5))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 5, 5))

        self.assertEqual(state.current_medications(self.patient), [])
        self.assertEqual(state.discontinued_medications(self.patient), [])

    def test_a_conflicted_medication_is_not_silently_dropped(self):
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 5, 5))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 5, 5))

        self.assertEqual(len(state.conflicted_medications(self.patient)), 1)

    def test_a_later_change_is_not_a_conflict(self):
        """Disagreement is only meaningful between statements of the same date."""
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 1, 1))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 6, 1))

        self.assertEqual(state.conflicted_medications(self.patient), [])

    def test_agreeing_documents_on_one_date_are_not_a_conflict(self):
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 5, 5))
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 5, 5))

        self.assertEqual(state.conflicted_medications(self.patient), [])
        self.assertEqual(len(state.current_medications(self.patient)), 1)

    def test_a_later_dated_document_supersedes_a_conflict(self):
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 5, 5))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 5, 5))
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 9, 9))

        self.assertEqual(state.conflicted_medications(self.patient), [])
        self.assertEqual(len(state.current_medications(self.patient)), 1)


class UndatedTests(_State):
    """A statement that cannot be placed on the timeline must not lead it."""

    def test_a_dated_statement_outranks_an_undated_one(self):
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Undated note', record_type='other')
        MedicationStatement.objects.create(
            record=record, patient=self.patient, name='Metformin',
            status=MedicationStatement.Status.DISCONTINUED)
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 1, 1))

        self.assertEqual(len(state.current_medications(self.patient)), 1)

    def test_two_undated_documents_that_disagree_are_a_conflict(self):
        """
        Neither can supersede the other, so neither wins. The alternative is
        letting insertion order decide whether someone is on a drug.
        """
        for status in (MedicationStatement.Status.ACTIVE,
                       MedicationStatement.Status.DISCONTINUED):
            record = MedicalRecord.objects.create(
                patient=self.patient, title='Undated', record_type='other')
            MedicationStatement.objects.create(
                record=record, patient=self.patient, name='Metformin', status=status)

        self.assertEqual(len(state.conflicted_medications(self.patient)), 1)
        self.assertEqual(state.current_medications(self.patient), [])

    def test_an_undated_statement_alone_is_reported_as_undated(self):
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Undated', record_type='other')
        MedicationStatement.objects.create(
            record=record, patient=self.patient, name='Metformin')

        result = state.medication_state(self.patient)['metformin']
        self.assertTrue(result.is_undated)


class ConditionTests(_State):

    def test_a_condition_is_current(self):
        self._cond()
        self.assertEqual(len(state.current_conditions(self.patient)), 1)

    def test_a_resolved_condition_leaves_the_current_list(self):
        self._cond(status=ConditionStatement.Status.ACTIVE, when=date(2026, 1, 1))
        self._cond(status=ConditionStatement.Status.RESOLVED, when=date(2026, 6, 1))

        self.assertEqual(state.current_conditions(self.patient), [])
        self.assertEqual(len(state.resolved_conditions(self.patient)), 1)

    def test_a_recurring_condition_can_become_current_again(self):
        self._cond(status=ConditionStatement.Status.ACTIVE, when=date(2026, 1, 1))
        self._cond(status=ConditionStatement.Status.RESOLVED, when=date(2026, 3, 1))
        self._cond(status=ConditionStatement.Status.ACTIVE, when=date(2026, 8, 1))

        self.assertEqual(len(state.current_conditions(self.patient)), 1)

    def test_condition_conflicts_are_reported(self):
        self._cond(status=ConditionStatement.Status.ACTIVE, when=date(2026, 5, 5))
        self._cond(status=ConditionStatement.Status.RESOLVED, when=date(2026, 5, 5))

        self.assertEqual(len(state.conflicted_conditions(self.patient)), 1)
        self.assertEqual(state.current_conditions(self.patient), [])

    def test_history_is_available(self):
        self._cond(when=date(2026, 1, 1))
        self._cond(status=ConditionStatement.Status.RESOLVED, when=date(2026, 6, 1))

        history = state.condition_history(self.patient, 'Type 2 diabetes')
        self.assertEqual(len(history), 2)


class ProvenanceTests(_State):

    def test_every_statement_names_the_document_that_asserted_it(self):
        record = self._record(title='Cardiology discharge')
        self._med(record=record)

        statement = state.current_medications(self.patient)[0].statement
        self.assertEqual(statement.record, record)
        self.assertEqual(statement.record.title, 'Cardiology discharge')

    def test_the_assertion_date_comes_from_the_document(self):
        self._med(when=date(2025, 12, 24))
        self.assertEqual(
            state.current_medications(self.patient)[0].statement.asserted_on,
            date(2025, 12, 24))

    def test_the_owner_is_derived_from_the_record(self):
        """Same rule as ParsedLabValue: the record is the source of truth."""
        other = User.objects.create_user(
            'st_other', email='st_other@test.invalid', password='pw', role='patient')
        record = self._record()
        statement = MedicationStatement.objects.create(
            record=record, patient=other, name='Metformin')

        statement.refresh_from_db()
        self.assertEqual(statement.patient, self.patient)


class IsolationTests(_State):

    def test_another_patients_medications_are_not_returned(self):
        other = User.objects.create_user(
            'st_other2', email='st_other2@test.invalid', password='pw', role='patient')
        other_record = MedicalRecord.objects.create(
            patient=other, title='Theirs', record_type='discharge',
            record_date=date(2026, 1, 1))
        MedicationStatement.objects.create(
            record=other_record, patient=other, name='Warfarin')
        self._med(name='Metformin')

        names = [r.statement.name for r in state.current_medications(self.patient)]
        self.assertEqual(names, ['Metformin'])


class DeletionTests(_State):

    def test_deleting_the_record_removes_its_statements(self):
        record = self._record()
        self._med(record=record)

        record.delete()
        self.assertEqual(MedicationStatement.objects.count(), 0)

    def test_erasing_the_patient_removes_their_statements(self):
        from apps.accounts.services import purge_user_data

        self._med()
        self._cond()

        with self.captureOnCommitCallbacks(execute=True):
            purge_user_data(self.patient)

        self.assertEqual(MedicationStatement.objects.count(), 0)
        self.assertEqual(ConditionStatement.objects.count(), 0)


class IngestionTests(TestCase):
    """The parser's output stops being discarded."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'st_ing', email='st_ing@test.invalid', password='pw', role='patient')

    def _ingest(self, structured):
        from apps.medical_records.services import _save_clinical_state

        record = MedicalRecord.objects.create(
            patient=self.patient, title='Summary', record_type='discharge',
            record_date=date(2026, 2, 2), parsed_data=structured)
        return record, _save_clinical_state(record, structured)

    def test_extracted_medications_are_stored(self):
        """ACCEPTANCE — a repo-wide search found zero consumers of this data."""
        _, created = self._ingest({'medications': [
            {'name': 'Metformin', 'dose': '500mg', 'frequency': 'twice daily'},
            {'name': 'Ramipril', 'dose': '5mg'},
        ]})

        self.assertEqual(created, 2)
        self.assertEqual(len(state.current_medications(self.patient)), 2)

    def test_extracted_diagnoses_are_stored(self):
        _, created = self._ingest({'diagnoses': [
            {'code': 'E11', 'description': 'Type 2 diabetes mellitus'},
        ]})

        self.assertEqual(created, 1)
        self.assertEqual(state.current_conditions(self.patient)[0].statement.code, 'E11')

    def test_a_diagnosis_with_only_a_code_is_kept(self):
        """
        The extraction schema is {"code": "", "description": ""} and the model
        fills whichever it can. Requiring the wording threw away real diagnoses.
        """
        _, created = self._ingest({'diagnoses': [{'code': 'E11'}]})

        self.assertEqual(created, 1)
        self.assertEqual(state.current_conditions(self.patient)[0].statement.code, 'E11')

    def test_code_only_diagnoses_do_not_merge_into_one(self):
        """
        ACCEPTANCE — keyed on the empty description, every code-only condition
        became the same condition, and one would supersede the others.
        """
        self._ingest({'diagnoses': [{'code': 'E11'}, {'code': 'I10'}]})

        codes = sorted(r.statement.code for r in state.current_conditions(self.patient))
        self.assertEqual(codes, ['E11', 'I10'])

    def test_a_diagnosis_with_neither_code_nor_wording_asserts_nothing(self):
        _, created = self._ingest({'diagnoses': [{'code': '', 'description': ''}]})
        self.assertEqual(created, 0)

    def test_a_nameless_medication_asserts_nothing(self):
        _, created = self._ingest({'medications': [{'dose': '500mg'}]})
        self.assertEqual(created, 0)

    def test_malformed_entries_do_not_break_ingestion(self):
        """A parsing problem is not a reason to reject the document."""
        _, created = self._ingest({'medications': ['not a dict', None, {'name': 'Aspirin'}]})
        self.assertEqual(created, 1)

    def test_a_discontinued_status_from_the_parser_is_respected(self):
        self._ingest({'medications': [{'name': 'Metformin', 'status': 'discontinued'}]})
        self.assertEqual(state.current_medications(self.patient), [])

    def test_absent_sections_are_harmless(self):
        _, created = self._ingest({'lab_values': []})
        self.assertEqual(created, 0)

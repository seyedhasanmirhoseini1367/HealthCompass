"""
REGRESSION — DM-2 (no owner on lab values) and DM-3 (no deterministic order).

DM-2 · Isolation rested on every caller remembering a join
-----------------------------------------------------------
`ParsedLabValue` and `WearableDataPoint` had no `patient` field. Every
patient-scoped query had to go through `record__patient`, and every caller did —
but nothing enforced it. A single `ParsedLabValue.objects.filter(
parameter_name='Creatinine')` written later, in a report or an export, would
have silently returned every patient's values. `MedicalDocument` and
`MedicalChunk` already denormalised the patient, so the codebase was also
inconsistent about it.

The field is derived, never independently set: `save()` takes it from the parent
record, so the two cannot drift. bulk_create bypasses `save()`, so those call
sites set it explicitly and a test here checks they do.

DM-3 · Row order was whatever the engine returned
--------------------------------------------------
Neither model declared `Meta.ordering`, so "the values on this record" came back
in an order that differs between the SQLite used in development and the Postgres
used in production. The doctor's record page, the export and the API serializers
all read those querysets. Ordering is now chronological with an explicit
tiebreak, and NULL handling is stated rather than inherited from the engine
(SQLite sorts NULLs first ascending, Postgres sorts them last).
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.medical_records.models import (MedicalRecord, ParsedLabValue,
                                         WearableDataPoint)

User = get_user_model()


class LabValueOwnerTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'dm_pat', email='dm_pat@test.invalid', password='pw', role='patient')
        self.other = User.objects.create_user(
            'dm_other', email='dm_other@test.invalid', password='pw', role='patient')
        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')

    def _value(self, name='Creatinine', **kwargs):
        return ParsedLabValue.objects.create(
            record=self.record, parameter_name=name, value='88', unit='µmol/L', **kwargs)

    def test_the_owner_is_filled_in_from_the_record(self):
        """ACCEPTANCE — DM-2. Callers do not have to remember."""
        self.assertEqual(self._value().patient, self.patient)

    def test_a_wrong_owner_is_corrected_not_trusted(self):
        """The parent record is the single source of truth for ownership."""
        value = ParsedLabValue.objects.create(
            record=self.record, patient=self.other,
            parameter_name='Creatinine', value='88')
        value.refresh_from_db()
        self.assertEqual(value.patient, self.patient)

    def test_a_patient_scoped_query_needs_no_join(self):
        self._value()
        self.assertEqual(
            ParsedLabValue.objects.filter(patient=self.patient).count(), 1)
        self.assertEqual(
            ParsedLabValue.objects.filter(patient=self.other).count(), 0)

    def test_the_direct_filter_agrees_with_the_join(self):
        """
        The whole point: the shortcut and the long way round must never give
        different answers.
        """
        self._value('Creatinine')
        self._value('Glucose')
        MedicalRecord.objects.create(
            patient=self.other, title='Other panel', record_type='lab_result'
        ).lab_values.create(parameter_name='Creatinine', value='70')

        direct = set(ParsedLabValue.objects.filter(
            patient=self.patient).values_list('pk', flat=True))
        joined = set(ParsedLabValue.objects.filter(
            record__patient=self.patient).values_list('pk', flat=True))
        self.assertEqual(direct, joined)

    def test_related_manager_creation_also_sets_the_owner(self):
        value = self.record.lab_values.create(parameter_name='Glucose', value='5.2')
        self.assertEqual(value.patient, self.patient)

    def test_erasing_the_patient_removes_their_values(self):
        self._value()
        self.patient.delete()
        self.assertEqual(ParsedLabValue.objects.count(), 0)

    def test_every_row_agrees_with_its_record(self):
        """Invariant sweep — no row may claim an owner its record does not."""
        from django.db.models import F

        self._value('Creatinine')
        self._value('Glucose')
        mismatched = (ParsedLabValue.objects
                      .exclude(patient__isnull=True)
                      .exclude(patient_id=F('record__patient_id'))
                      .count())
        self.assertEqual(mismatched, 0)


class WearablePointOwnerTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'dm_wear', email='dm_wear@test.invalid', password='pw', role='patient')
        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Watch', record_type='wearable')

    def test_the_owner_is_filled_in_from_the_record(self):
        point = WearableDataPoint.objects.create(
            record=self.record, metric='heart_rate', value=62,
            recorded_at=timezone.now())
        self.assertEqual(point.patient, self.patient)

    def test_bulk_created_points_carry_the_owner(self):
        """
        ACCEPTANCE — DM-2. bulk_create skips save(), so the import path sets it
        explicitly. A row without an owner is invisible to every scoped query.
        """
        now = timezone.now()
        WearableDataPoint.objects.bulk_create([
            WearableDataPoint(record=self.record, patient=self.patient,
                              metric='steps', value=n * 100, recorded_at=now)
            for n in range(3)
        ])
        self.assertEqual(
            WearableDataPoint.objects.filter(patient=self.patient).count(), 3)

    def test_the_csv_import_path_sets_the_owner(self):
        """The real ingestion path, not a hand-built object."""
        from apps.medical_records.services import MedicalRecordService

        # One column per metric, which is the shape real exports use.
        csv = (b'date,heart_rate,steps\n'
               b'2026-01-01T08:00:00,62,1200\n'
               b'2026-01-01T09:00:00,71,3400\n')
        result = MedicalRecordService.create_from_wearable(
            self.patient, csv, filename='watch.csv')
        self.assertNotIn('error', result, result.get('error'))

        points = WearableDataPoint.objects.filter(record=result['record'])
        self.assertTrue(points.exists())
        self.assertFalse(points.filter(patient__isnull=True).exists(),
                         'imported points have no owner')


class OrderingTests(TestCase):
    """DM-3 — the same query must give the same order on every engine."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'dm_ord', email='dm_ord@test.invalid', password='pw', role='patient')
        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')
        self.now = timezone.now()

    def test_lab_values_come_back_chronologically(self):
        for offset in (2, 0, 1):
            ParsedLabValue.objects.create(
                record=self.record, parameter_name=f'A{offset}', value='1',
                measured_at=self.now + timedelta(days=offset))

        order = [v.parameter_name for v in self.record.lab_values.all()]
        self.assertEqual(order, ['A0', 'A1', 'A2'])

    def test_undated_values_sort_last_not_first(self):
        """
        ACCEPTANCE — DM-3. SQLite sorted NULLs first and Postgres last, so this
        differed between development and production.
        """
        ParsedLabValue.objects.create(
            record=self.record, parameter_name='undated', value='1')
        ParsedLabValue.objects.create(
            record=self.record, parameter_name='dated', value='1',
            measured_at=self.now)

        order = [v.parameter_name for v in self.record.lab_values.all()]
        self.assertEqual(order, ['dated', 'undated'])

    def test_same_timestamp_values_have_a_stable_tiebreak(self):
        for name in ('first', 'second', 'third'):
            ParsedLabValue.objects.create(
                record=self.record, parameter_name=name, value='1',
                measured_at=self.now)

        first_pass = [v.parameter_name for v in self.record.lab_values.all()]
        second_pass = [v.parameter_name for v in self.record.lab_values.all()]
        self.assertEqual(first_pass, second_pass)
        self.assertEqual(first_pass, ['first', 'second', 'third'])

    def test_wearable_points_are_ordered_and_tiebroken(self):
        for offset in (2, 0, 1):
            WearableDataPoint.objects.create(
                record=self.record, metric='steps', value=offset,
                recorded_at=self.now + timedelta(hours=offset))
        values = [p.value for p in self.record.wearable_points.all()]
        self.assertEqual(values, [0, 1, 2])

    def test_both_models_declare_an_ordering(self):
        for model in (ParsedLabValue, WearableDataPoint):
            with self.subTest(model=model.__name__):
                self.assertTrue(model._meta.ordering,
                                f'{model.__name__} has no Meta.ordering')

    def test_the_clinical_fact_layer_still_orders_explicitly(self):
        """
        Meta.ordering is a safety net for casual callers. The clinical layer
        must not start depending on it — it states its own order because the
        tiebreak between two readings on one date is a clinical decision.
        """
        import inspect

        from apps.medical_records import clinical_facts
        self.assertIn('order_by', inspect.getsource(clinical_facts.series))

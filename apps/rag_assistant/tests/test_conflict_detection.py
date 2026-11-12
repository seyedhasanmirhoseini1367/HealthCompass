"""
ACCEPTANCE — FINDING R6: conflict vs progression vs duplicate.

The value of this feature is the distinction it refuses to blur. Different
values on different dates are a person's history; calling that "your records
disagree" would be both wrong and frightening. Only a same-analyte, same-date
disagreement is a conflict.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

User = get_user_model()
NO_AUTOINDEX = override_settings(RAG_AUTO_INDEX_SYNC=False)


@NO_AUTOINDEX
class ConflictDetectionTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='cf', email='cf@example.com', password='pw-cf-1',
        )

    def _add(self, title, day, parameter, value, unit='mmol/L',
             canonical=None, unit_known=True):
        from apps.medical_records.models import MedicalRecord, ParsedLabValue

        record = MedicalRecord.objects.create(
            patient=self.user, title=title, record_type='lab_result',
            record_date=date.fromisoformat(day),
        )
        return ParsedLabValue.objects.create(
            record=record, parameter_name=parameter, value=str(value), unit=unit,
            canonical_value=float(value) if canonical is None else canonical,
            unit_known=unit_known,
            measured_at=timezone.make_aware(
                timezone.datetime.fromisoformat(day + 'T09:00:00')),
        )

    def _groups(self):
        from apps.rag_assistant.services.conflict_service import analyze_lab_values
        return {g['parameter']: g for g in analyze_lab_values(self.user)}

    # ── the three outcomes ───────────────────────────────────────────────────

    def test_different_values_on_different_dates_is_progression(self):
        self._add('Panel 2024', '2024-03-11', 'Glucose', 5.1)
        self._add('Panel 2025', '2025-04-02', 'Glucose', 6.4)
        self._add('Panel 2026', '2026-05-20', 'Glucose', 7.8)

        group = self._groups()['glucose']
        self.assertEqual(group['status'], 'progression')
        self.assertEqual(group['conflicts'], [])
        self.assertEqual(len(group['observations']), 3)

    def test_different_values_on_the_same_date_is_a_conflict(self):
        self._add('Lab A', '2026-05-20', 'Glucose', 7.8)
        self._add('Lab B', '2026-05-20', 'Glucose', 5.2)

        group = self._groups()['glucose']
        self.assertEqual(group['status'], 'conflict')
        self.assertEqual(len(group['conflicts']), 1)
        self.assertEqual(group['conflicts'][0]['date'], '2026-05-20')
        self.assertEqual(sorted(group['conflicts'][0]['values']), [5.2, 7.8])

    def test_same_value_on_the_same_date_is_a_duplicate(self):
        self._add('Lab A', '2026-05-20', 'Glucose', 7.8)
        self._add('Lab A copy', '2026-05-20', 'Glucose', 7.8)

        group = self._groups()['glucose']
        self.assertEqual(group['status'], 'duplicate')
        self.assertEqual(group['conflicts'], [])

    # ── properties that keep it honest ───────────────────────────────────────

    def test_conflicts_preserve_dates_and_sources(self):
        self._add('Hospital Lab', '2026-05-20', 'Glucose', 7.8)
        self._add('Clinic Lab', '2026-05-20', 'Glucose', 5.2)

        conflict = self._groups()['glucose']['conflicts'][0]
        titles = {s['record_title'] for s in conflict['sources']}
        self.assertEqual(titles, {'Hospital Lab', 'Clinic Lab'})
        for source in conflict['sources']:
            self.assertIsNotNone(source['record_id'])

    def test_unrecognised_units_are_never_called_a_conflict(self):
        """An uncomparable value is not evidence of disagreement."""
        self._add('Lab A', '2026-05-20', 'Glucose', 7.8)
        self._add('Lab B', '2026-05-20', 'Glucose', 140, unit='mg/dL',
                  canonical=None, unit_known=False)

        self.assertNotEqual(self._groups()['glucose']['status'], 'conflict')

    def test_analytes_are_grouped_independently(self):
        self._add('Panel', '2026-05-20', 'Glucose', 7.8)
        self._add('Panel', '2026-05-20', 'Creatinine', 142, unit='umol/L')

        groups = self._groups()
        self.assertEqual(set(groups), {'glucose', 'creatinine'})
        for group in groups.values():
            self.assertEqual(group['status'], 'single')

    def test_parameter_names_group_case_insensitively(self):
        self._add('Lab A', '2026-05-20', 'Glucose', 7.8)
        self._add('Lab B', '2026-05-20', 'glucose', 5.2)
        self.assertEqual(set(self._groups()), {'glucose'})

    def test_analysis_is_scoped_to_the_patient(self):
        from apps.medical_records.models import MedicalRecord, ParsedLabValue

        other = User.objects.create_user(
            username='cf-other', email='cfo@example.com', password='pw-cfo-1',
        )
        record = MedicalRecord.objects.create(
            patient=other, title='Theirs', record_type='lab_result',
            record_date=date(2026, 5, 20),
        )
        ParsedLabValue.objects.create(
            record=record, parameter_name='Glucose', value='99.9', unit='mmol/L',
            canonical_value=99.9, measured_at=timezone.now(),
        )
        self._add('Mine', '2026-05-20', 'Glucose', 7.8)

        values = [o['value'] for o in self._groups()['glucose']['observations']]
        self.assertNotIn('99.9', values)

    # ── what reaches the model ───────────────────────────────────────────────

    def test_notice_is_emitted_only_for_real_conflicts(self):
        from apps.rag_assistant.services.conflict_service import (
            analyze_lab_values, format_conflict_notice)

        self._add('Panel 2024', '2024-03-11', 'Glucose', 5.1)
        self._add('Panel 2026', '2026-05-20', 'Glucose', 7.8)
        self.assertEqual(format_conflict_notice(analyze_lab_values(self.user)), '')

        self._add('Clinic Lab', '2026-05-20', 'Glucose', 5.2)
        notice = format_conflict_notice(analyze_lab_values(self.user))
        self.assertIn('CONFLICTING RECORDS', notice)
        self.assertIn('Clinic Lab', notice)
        self.assertIn('2026-05-20', notice)

    def test_notice_does_not_resolve_the_conflict(self):
        """Nothing in the data says which record is right."""
        from apps.rag_assistant.services.conflict_service import (
            analyze_lab_values, format_conflict_notice)

        self._add('Lab A', '2026-05-20', 'Glucose', 7.8)
        self._add('Lab B', '2026-05-20', 'Glucose', 5.2)

        notice = format_conflict_notice(analyze_lab_values(self.user)).lower()
        for verdict in ('is correct', 'more likely', 'ignore the', 'use the newer'):
            self.assertNotIn(verdict, notice)

    def test_trajectory_context_carries_the_conflict_notice(self):
        from apps.rag_assistant.services.trajectory_service import TrajectoryService

        self._add('Hospital Lab', '2026-05-20', 'Glucose', 7.8)
        self._add('Clinic Lab', '2026-05-20', 'Glucose', 5.2)

        context, _ = TrajectoryService().get_trajectory_context(
            self.user, 'my glucose', temporal_mode='latest')
        self.assertIn('CONFLICTING RECORDS', context)

    def test_progression_does_not_pollute_the_trajectory_context(self):
        from apps.rag_assistant.services.trajectory_service import TrajectoryService

        self._add('Panel 2024', '2024-03-11', 'Glucose', 5.1)
        self._add('Panel 2026', '2026-05-20', 'Glucose', 7.8)

        context, _ = TrajectoryService().get_trajectory_context(
            self.user, 'my glucose', temporal_mode='trend')
        self.assertNotIn('CONFLICTING RECORDS', context)

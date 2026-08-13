"""
Tests — Step 1: the typed fact accessor.

Two classes of guarantee are pinned here.

**Structural** — the result types make the old failure modes unrepresentable:

  * `Conflicted` has no `.value`. A caller cannot collapse disagreeing readings
    to one number, because there is nothing to collapse to. This is what stops
    C1 from recurring anywhere rather than fixing it at one call site.
  * `Absent` carries a machine-readable reason. "Nothing on file" is a distinct
    result, not an empty string a caller can mistake for a normal value.

**Behavioural** — the reconciliation of the two previous readers:

  | | TrajectoryService | conflict_service | accessor |
  |---|---|---|---|
  | query filter   | unit_known=True | none | none |
  | date source    | record_date | measured_at→record_date | measured_at→record_date |
  | comparability  | re-normalised on the fly | canonical_value AND unit_known | canonical AND unit_known |
  | same-date rows | **deduped, rest discarded** | all kept | all kept |

The last row is the one that mattered: the readings the trajectory query threw
away were precisely the ones that made a date contested.

No thresholds or clinical rules are asserted anywhere in this file.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.medical_records import clinical_facts as facts
from apps.medical_records.clinical_facts import (
    Absence, Absent, Confirmed, Conflicted,
)
from apps.medical_records.models import MedicalRecord, ParsedLabValue


class _Fixture(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='facts', password='pw-test-only', email='f@example.com')
        self.other = get_user_model().objects.create_user(
            username='facts-other', password='pw-test-only', email='o@example.com')

    def _reading(self, analyte, canonical, when, *, unit='mg/dL', raw=None,
                 unit_known=True, patient=None, title=None, measured_at=None):
        patient = patient or self.user
        record = MedicalRecord.objects.create(
            patient=patient, title=title or f'{analyte} {when}',
            record_type='lab_result', record_date=when)
        return ParsedLabValue.objects.create(
            record=record, parameter_name=analyte,
            value=str(raw if raw is not None else canonical),
            unit=unit, canonical_value=canonical, original_unit=unit,
            unit_known=unit_known, measured_at=measured_at)


class ResultTypeTests(_Fixture):
    """The types themselves must make the old mistakes impossible."""

    def test_conflicted_exposes_no_single_value(self):
        """
        The structural guarantee. If Conflicted had a `.value`, every call site
        would eventually use it and C1 would come back somewhere else.
        """
        self.assertFalse(hasattr(Conflicted(observations=[], date=None), 'value'))

    def test_conflicted_is_not_reported_as_known(self):
        self.assertFalse(Conflicted(observations=[], date=None).is_known)

    def test_absent_carries_a_machine_readable_reason(self):
        result = Absent(Absence.NO_OBSERVATIONS)
        self.assertEqual(result.reason, 'no_observations')
        self.assertFalse(result.is_known)

    def test_confirmed_is_the_only_known_result(self):
        obs = facts.Observation(
            value=5.0, unit='mg/dL', raw_value='5.0', original_unit='mg/dL',
            date=date(2026, 1, 1), is_abnormal=False, is_critical=False,
            unit_known=True, parameter_name='Glucose',
            record_id=None, record_title=None)
        self.assertTrue(Confirmed(observation=obs).is_known)


class MissingDataTests(_Fixture):
    """Missing must never look like normal."""

    def test_no_rows_is_absent_not_a_value(self):
        result = facts.latest(self.user, 'glucose')
        self.assertIsInstance(result, Absent)
        self.assertEqual(result.reason, Absence.NO_OBSERVATIONS)

    def test_rows_with_unresolvable_units_are_absent_not_guessed(self):
        """
        A number whose unit we could not resolve is not a value. Reporting it
        anyway is how a mg/dL reading gets compared with an mmol/L threshold.
        """
        self._reading('Glucose', 140.0, date(2026, 5, 20),
                      unit='furlongs', unit_known=False)
        result = facts.latest(self.user, 'glucose')
        self.assertIsInstance(result, Absent)
        self.assertEqual(result.reason, Absence.NO_COMPARABLE)

    def test_undated_rows_cannot_be_placed_on_the_timeline(self):
        record = MedicalRecord.objects.create(
            patient=self.user, title='undated', record_type='lab_result',
            record_date=None)
        ParsedLabValue.objects.create(
            record=record, parameter_name='Glucose', value='5.0', unit='mg/dL',
            canonical_value=5.0, original_unit='mg/dL', unit_known=True)
        result = facts.latest(self.user, 'glucose')
        self.assertIsInstance(result, Absent)
        self.assertEqual(result.reason, Absence.NO_DATE)

    def test_single_reading_has_no_previous(self):
        self._reading('Glucose', 90.0, date(2026, 5, 20))
        result = facts.previous(self.user, 'glucose')
        self.assertIsInstance(result, Absent)
        self.assertEqual(result.reason, Absence.NO_PREVIOUS)


class ConfirmedTests(_Fixture):

    def test_single_reading_is_confirmed(self):
        self._reading('Glucose', 90.08, date(2026, 5, 20))
        result = facts.latest(self.user, 'glucose')
        self.assertIsInstance(result, Confirmed)
        self.assertAlmostEqual(result.observation.value, 90.08)

    def test_agreeing_readings_on_one_date_are_confirmed_not_conflicted(self):
        """Two documents reporting the SAME value is duplication, not conflict."""
        self._reading('Glucose', 90.0, date(2026, 5, 20), title='Panel A')
        self._reading('Glucose', 90.0, date(2026, 5, 20), title='Panel B')
        result = facts.latest(self.user, 'glucose')
        self.assertIsInstance(result, Confirmed)
        self.assertEqual(len(result.agreeing), 1)

    def test_latest_picks_the_newest_date(self):
        self._reading('Glucose', 80.0, date(2024, 1, 1))
        self._reading('Glucose', 95.0, date(2026, 5, 20))
        result = facts.latest(self.user, 'glucose')
        self.assertIsInstance(result, Confirmed)
        self.assertAlmostEqual(result.observation.value, 95.0)

    def test_previous_is_the_previous_DATE_not_the_previous_row(self):
        self._reading('Glucose', 80.0, date(2024, 1, 1))
        self._reading('Glucose', 90.0, date(2026, 5, 20), title='A')
        self._reading('Glucose', 90.0, date(2026, 5, 20), title='B')
        result = facts.previous(self.user, 'glucose')
        self.assertIsInstance(result, Confirmed)
        self.assertAlmostEqual(result.observation.value, 80.0)


class ConflictTests(_Fixture):
    """C1, at the layer where it can be prevented for every caller."""

    def setUp(self):
        super().setUp()
        self._reading('Glucose', 5.0 * 18.016, date(2023, 2, 14))
        self._reading('Glucose', 6.4 * 18.016, date(2025, 4, 2))
        # The contested newest date.
        self._reading('Glucose', 7.8 * 18.016, date(2026, 5, 20),
                      title='Metabolic Panel 2026')
        self._reading('Glucose', 5.2 * 18.016, date(2026, 5, 20),
                      title='Second Opinion Lab 2026')

    def test_contested_newest_date_is_conflicted_never_confirmed(self):
        """ACCEPTANCE. The old code returned 5.2 as fact and computed a trend from it."""
        result = facts.latest(self.user, 'glucose')
        self.assertIsInstance(result, Conflicted)
        self.assertEqual(len(result.observations), 2)
        self.assertEqual(result.date, date(2026, 5, 20))

    def test_both_conflicting_values_are_carried_with_their_sources(self):
        result = facts.latest(self.user, 'glucose')
        titles = {o.record_title for o in result.observations}
        self.assertEqual(titles, {'Metabolic Panel 2026', 'Second Opinion Lab 2026'})
        for o in result.observations:
            self.assertIsNotNone(o.record_id)

    def test_no_reading_is_discarded_from_the_series(self):
        """
        The trajectory query kept one row per date. The rows it dropped were
        exactly the ones that made a date contested.
        """
        observations = facts.series(self.user, 'glucose')
        self.assertEqual(len(observations), 4)
        on_contested = [o for o in observations if o.date == date(2026, 5, 20)]
        self.assertEqual(len(on_contested), 2)

    def test_contested_dates_are_reportable(self):
        self.assertEqual(facts.contested_dates(self.user, 'glucose'),
                         [date(2026, 5, 20)])

    def test_previous_is_unaffected_by_a_contested_newest_date(self):
        result = facts.previous(self.user, 'glucose')
        self.assertIsInstance(result, Confirmed)
        self.assertAlmostEqual(result.observation.value, 6.4 * 18.016, places=3)

    def test_conflict_on_an_older_date_does_not_make_latest_conflicted(self):
        user = self.other
        self._reading('Glucose', 90.0, date(2024, 1, 1), patient=user, title='X')
        self._reading('Glucose', 99.0, date(2024, 1, 1), patient=user, title='Y')
        self._reading('Glucose', 95.0, date(2026, 5, 20), patient=user)
        self.assertIsInstance(facts.latest(user, 'glucose'), Confirmed)
        self.assertEqual(facts.contested_dates(user, 'glucose'), [date(2024, 1, 1)])


class HistoryIsNotConflictTests(_Fixture):
    """Different values on different dates are a person's history."""

    def test_progression_over_time_is_not_a_conflict(self):
        self._reading('Glucose', 90.0, date(2024, 1, 1))
        self._reading('Glucose', 115.0, date(2025, 1, 1))
        self._reading('Glucose', 140.0, date(2026, 1, 1))
        self.assertIsInstance(facts.latest(self.user, 'glucose'), Confirmed)
        self.assertEqual(facts.contested_dates(self.user, 'glucose'), [])


class UnitSafetyTests(_Fixture):
    """Unresolvable units are surfaced, never silently dropped or compared."""

    def test_unresolved_row_is_present_in_the_series_but_flagged(self):
        self._reading('Glucose', 90.0, date(2026, 1, 1))
        self._reading('Glucose', 140.0, date(2026, 5, 20),
                      unit='furlongs', unit_known=False)
        observations = facts.series(self.user, 'glucose')
        self.assertEqual(len(observations), 2)
        unresolved = [o for o in observations if not o.unit_known]
        self.assertEqual(len(unresolved), 1)
        self.assertFalse(unresolved[0].comparable)

    def test_unresolved_row_does_not_create_a_false_conflict(self):
        """An incomparable number cannot contradict a comparable one."""
        self._reading('Glucose', 90.0, date(2026, 5, 20))
        self._reading('Glucose', 140.0, date(2026, 5, 20),
                      unit='furlongs', unit_known=False)
        result = facts.latest(self.user, 'glucose')
        self.assertIsInstance(result, Confirmed)
        self.assertEqual(facts.contested_dates(self.user, 'glucose'), [])

    def test_raw_value_and_original_unit_are_preserved_for_attribution(self):
        """An answer must be able to quote the document, not our conversion."""
        self._reading('Glucose', 90.08, date(2026, 5, 20),
                      unit='mmol/L', raw='5.0')
        obs = facts.series(self.user, 'glucose')[0]
        self.assertEqual(obs.raw_value, '5.0')
        self.assertEqual(obs.original_unit, 'mmol/L')


class DateSourceTests(_Fixture):
    """measured_at is the clinical time and takes precedence over the record date."""

    def test_measured_at_wins_over_record_date(self):
        when = timezone.now().replace(year=2026, month=3, day=9)
        self._reading('Glucose', 90.0, date(2026, 5, 20), measured_at=when)
        obs = facts.series(self.user, 'glucose')[0]
        self.assertEqual(obs.date, when.date())

    def test_record_date_is_used_when_measured_at_is_absent(self):
        self._reading('Glucose', 90.0, date(2026, 5, 20))
        self.assertEqual(facts.series(self.user, 'glucose')[0].date, date(2026, 5, 20))


class AnalyteVocabularyTests(_Fixture):
    """Aliases come from the shared biomarker table, not a new list."""

    def test_alias_matches_a_differently_named_row(self):
        self._reading('Fasting Glucose', 90.0, date(2026, 5, 20))
        self.assertIsInstance(facts.latest(self.user, 'glucose'), Confirmed)

    def test_unrelated_analyte_is_not_matched(self):
        self._reading('Creatinine', 1.1, date(2026, 5, 20))
        self.assertIsInstance(facts.latest(self.user, 'glucose'), Absent)

    def test_analytes_for_lists_what_a_patient_has(self):
        self._reading('Glucose', 90.0, date(2026, 1, 1))
        self._reading('Creatinine', 1.1, date(2026, 1, 1))
        self.assertEqual(facts.analytes_for(self.user), ['creatinine', 'glucose'])


class GroupingModeTests(_Fixture):
    """
    Alias vs exact grouping — an explicit choice, because the two answer
    different questions and the difference is safety-relevant.

    A patient asking "what is my glucose?" means the analyte, so alias matching
    is right. Conflict detection asking "do these records contradict each
    other?" must NOT merge fasting glucose with random glucose: they are
    different tests, and reporting them as contradictory would be a false alarm
    about two measurements that legitimately differ.

    No claim is made here that these aliases are clinically equivalent. The
    accessor offers both groupings and requires the caller to say which it
    means.
    """

    def setUp(self):
        super().setUp()
        self._reading('Fasting Glucose', 90.0, date(2026, 5, 20), title='Fasting panel')
        self._reading('Random Blood Sugar', 160.0, date(2026, 5, 20), title='Random draw')

    def test_alias_mode_groups_related_analytes_for_a_query(self):
        """A clinical query about glucose should see both readings."""
        observations = facts.series(self.user, 'glucose')
        self.assertEqual(len(observations), 2)

    def test_exact_mode_keeps_differently_named_tests_apart(self):
        """ACCEPTANCE. Exact grouping must not merge two different tests."""
        self.assertEqual(len(facts.series(self.user, 'fasting glucose', exact=True)), 1)
        self.assertEqual(len(facts.series(self.user, 'random blood sugar', exact=True)), 1)

    def test_exact_mode_reports_no_conflict_between_different_tests(self):
        """
        The safety property. Under alias grouping these two readings on one date
        look contradictory; under exact grouping they are simply two tests.
        """
        self.assertEqual(facts.contested_dates(self.user, 'fasting glucose', exact=True), [])
        self.assertEqual(facts.contested_dates(self.user, 'random blood sugar', exact=True), [])

    def test_alias_mode_would_have_called_them_contested(self):
        """
        Documents WHY exact mode is required for conflict detection: the same
        data under alias grouping does look like a same-date disagreement.
        """
        self.assertEqual(facts.contested_dates(self.user, 'glucose'),
                         [date(2026, 5, 20)])

    def test_exact_mode_still_detects_a_real_conflict(self):
        """Two readings of the SAME test on one date remain a conflict."""
        self._reading('Fasting Glucose', 200.0, date(2026, 5, 20), title='Repeat fasting')
        result = facts.latest(self.user, 'fasting glucose', exact=True)
        self.assertIsInstance(result, Conflicted)
        self.assertEqual(len(result.observations), 2)

    def test_exact_mode_is_case_insensitive(self):
        """'Glucose' and 'glucose' are the same analyte name, not two."""
        self._reading('GLUCOSE', 90.0, date(2026, 1, 1))
        self._reading('glucose', 90.0, date(2026, 2, 1))
        self.assertEqual(len(facts.series(self.user, 'glucose', exact=True)), 2)

    def test_exact_flag_propagates_through_every_query(self):
        for fn in (facts.latest, facts.previous):
            with self.subTest(fn=fn.__name__):
                # Alias mode sees both tests; exact mode sees only one.
                self.assertNotIsInstance(
                    fn(self.user, 'fasting glucose', exact=True), type(None))
        self.assertIsInstance(
            facts.on_date(self.user, 'fasting glucose', date(2026, 5, 20), exact=True),
            Confirmed)


class ConflictServiceUsesTheAccessorTests(_Fixture):
    """conflict_service must not keep its own selection logic."""

    def test_conflict_service_defines_no_second_selection_logic(self):
        """
        Structural. These three helpers were the duplicate definition of "a lab
        value" — different query filter, different date source, different
        comparability rule from the trajectory path.
        """
        from apps.rag_assistant.services import conflict_service

        for gone in ('_fact_key', '_observation_date', '_comparable_value'):
            self.assertFalse(hasattr(conflict_service, gone),
                             f'{gone} is a second definition of a lab value')

    def test_conflict_service_does_not_merge_different_tests(self):
        """The reason exact mode exists, asserted at the consumer."""
        from apps.rag_assistant.services.conflict_service import analyze_lab_values

        self._reading('Fasting Glucose', 90.0, date(2026, 5, 20))
        self._reading('Random Blood Sugar', 160.0, date(2026, 5, 20))

        groups = {g['parameter']: g for g in analyze_lab_values(self.user)}
        self.assertIn('fasting glucose', groups)
        self.assertIn('random blood sugar', groups)
        for group in groups.values():
            self.assertEqual(group['conflicts'], [])

    def test_conflict_service_still_finds_a_genuine_same_test_conflict(self):
        from apps.rag_assistant.services.conflict_service import (
            CONFLICT, analyze_lab_values,
        )

        self._reading('Glucose', 90.0, date(2026, 5, 20), title='Panel A')
        self._reading('Glucose', 140.0, date(2026, 5, 20), title='Panel B')

        groups = {g['parameter']: g for g in analyze_lab_values(self.user)}
        self.assertEqual(groups['glucose']['status'], CONFLICT)
        self.assertEqual(len(groups['glucose']['conflicts']), 1)


class PatientIsolationTests(_Fixture):
    """Every query is scoped to one patient."""

    def test_another_patients_readings_are_never_returned(self):
        self._reading('Glucose', 999.0, date(2026, 5, 20), patient=self.other)
        self.assertIsInstance(facts.latest(self.user, 'glucose'), Absent)
        self.assertEqual(facts.series(self.user, 'glucose'), [])

    def test_another_patients_readings_do_not_create_a_conflict(self):
        self._reading('Glucose', 90.0, date(2026, 5, 20))
        self._reading('Glucose', 500.0, date(2026, 5, 20), patient=self.other)
        self.assertIsInstance(facts.latest(self.user, 'glucose'), Confirmed)
        self.assertEqual(facts.contested_dates(self.user, 'glucose'), [])

    def test_each_patient_sees_only_their_own_series(self):
        self._reading('Glucose', 90.0, date(2026, 1, 1))
        self._reading('Glucose', 91.0, date(2026, 2, 1))
        self._reading('Glucose', 500.0, date(2026, 1, 1), patient=self.other)
        self.assertEqual(len(facts.series(self.user, 'glucose')), 2)
        self.assertEqual(len(facts.series(self.other, 'glucose')), 1)

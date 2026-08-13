"""
REGRESSION — CB-1: critical-value detection must use the canonical value.

`_check_critical()` compared the RAW uploaded number against thresholds written
in SI units, before normalisation had run:

    'glucose': (value < 2.5 or value > 25)      # mmol/L thresholds

Canonical glucose is mg/dL, so a US-format record reporting 140 mg/dL evaluated
`140 > 25` and was flagged CRITICAL — firing a HealthAlert and a push
notification for an ordinary post-meal reading. The check also ran on the Kanta
path only (services.py:362); the PDF and text paths computed criticality never,
so whether a genuinely critical value was noticed depended on the file format.

Two further defects surfaced while fixing it, both covered below:

  * Naively switching to canonical_value with the OLD numbers would flag every
    Finnish glucose: 5.0 mmol/L normalises to 90.08 mg/dL, and 90 > 25.
    Thresholds therefore had to be converted, not merely re-pointed.
  * Analyte matching is by substring, and 'hemoglobin' is a substring of
    'hemoglobin a1c' — so an HbA1c of 5.4 % hit the hemoglobin rule
    (5.4 < 70) and was reported CRITICAL.

Thresholds are unchanged clinically: they are declared in their original SI
units and converted through `normalize()` itself, so the factor cannot drift
from the one applied to real values.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.medical_records.models import MedicalRecord, ParsedLabValue
from apps.medical_records.services import (
    _CRITICAL_THRESHOLDS, _is_critical, _save_lab_value,
)


class ThresholdDerivationTests(TestCase):
    """The converted thresholds must equal the SI ones, in canonical units."""

    def test_glucose_thresholds_are_in_mg_dl(self):
        low, high, unit = _CRITICAL_THRESHOLDS['glucose']
        self.assertEqual(unit, 'mg/dl')
        self.assertAlmostEqual(low, 2.5 * 18.016, places=3)    # was 2.5 mmol/L
        self.assertAlmostEqual(high, 25.0 * 18.016, places=3)  # was 25 mmol/L

    def test_creatinine_threshold_is_in_mg_dl(self):
        low, high, unit = _CRITICAL_THRESHOLDS['creatinine']
        self.assertEqual(unit, 'mg/dl')
        self.assertIsNone(low)
        self.assertAlmostEqual(high, 1000.0 / 88.4, places=3)  # was 1000 µmol/L

    def test_hemoglobin_thresholds_are_in_g_dl(self):
        low, high, unit = _CRITICAL_THRESHOLDS['hemoglobin']
        self.assertEqual(unit, 'g/dl')
        self.assertAlmostEqual(low, 7.0)     # was 70 g/L
        self.assertAlmostEqual(high, 20.0)   # was 200 g/L

    def test_troponin_is_absent_until_its_unit_is_established(self):
        """
        Removed deliberately: no conversion entry and no other reference exists,
        so ng/mL vs ng/L (1000x apart) cannot be told apart. Comparing anyway is
        the exact defect CB-1 is about.
        """
        self.assertNotIn('troponin', _CRITICAL_THRESHOLDS)


class CanonicalComparisonTests(TestCase):
    """The headline cases from the audit."""

    def test_glucose_140_mg_dl_is_not_critical(self):
        """ACCEPTANCE — CB-1. Was CRITICAL because 140 > 25 (an mmol/L bound)."""
        self.assertFalse(_is_critical('Glucose', 140.0, 'mg/dL', True))

    def test_normal_si_glucose_is_not_critical_after_normalisation(self):
        """5.0 mmol/L -> 90.08 mg/dL. Guards the naive re-point of thresholds."""
        self.assertFalse(_is_critical('Glucose', 5.0 * 18.016, 'mg/dL', True))

    def test_genuinely_critical_glucose_in_mg_dl_is_detected(self):
        self.assertTrue(_is_critical('Glucose', 500.0, 'mg/dL', True))

    def test_si_and_conventional_forms_agree(self):
        """The same measurement must classify identically in either unit."""
        for si_mmol in (2.0, 5.0, 12.0, 30.0):
            conventional = si_mmol * 18.016
            with self.subTest(mmol=si_mmol):
                self.assertEqual(
                    _is_critical('Glucose', conventional, 'mg/dL', True),
                    (si_mmol < 2.5 or si_mmol > 25.0),
                )

    def test_low_glucose_is_still_critical(self):
        self.assertTrue(_is_critical('Glucose', 2.0 * 18.016, 'mg/dL', True))

    def test_creatinine_above_and_below_the_bound(self):
        self.assertTrue(_is_critical('Creatinine', 1200.0 / 88.4, 'mg/dL', True))
        self.assertFalse(_is_critical('Creatinine', 100.0 / 88.4, 'mg/dL', True))

    def test_hemoglobin_bounds(self):
        self.assertTrue(_is_critical('Hemoglobin', 6.0, 'g/dL', True))    # 60 g/L
        self.assertFalse(_is_critical('Hemoglobin', 14.0, 'g/dL', True))  # 140 g/L
        self.assertTrue(_is_critical('Hemoglobin', 21.0, 'g/dL', True))   # 210 g/L


class UnsafeComparisonTests(TestCase):
    """Never compare across units. A missed alert beats a wrong one."""

    def test_unknown_unit_is_not_critical(self):
        self.assertFalse(_is_critical('Glucose', 140.0, 'lolunits', True))

    def test_unit_known_false_is_not_critical(self):
        """normalize() flags a known analyte whose unit it could not resolve."""
        self.assertFalse(_is_critical('Glucose', 999.0, 'mg/dL', False))

    def test_missing_unit_is_not_critical(self):
        self.assertFalse(_is_critical('Glucose', 999.0, '', True))

    def test_unparseable_value_is_not_critical(self):
        self.assertFalse(_is_critical('Glucose', None, 'mg/dL', True))

    def test_unknown_analyte_is_not_critical(self):
        self.assertFalse(_is_critical('Ferritin', 99999.0, 'ug/L', True))

    def test_hba1c_does_not_match_the_hemoglobin_rule(self):
        """'hemoglobin' is a substring of 'hemoglobin a1c'; 5.4 < 70 was CRITICAL."""
        self.assertFalse(_is_critical('Hemoglobin A1c', 5.4, '%', True))
        self.assertFalse(_is_critical('HbA1c', 5.4, '%', True))

    def test_potassium_accepts_meq_l_as_equivalent(self):
        """mEq/L == mmol/L for singly-charged ions; both must behave the same."""
        self.assertTrue(_is_critical('Potassium', 7.0, 'mmol/L', True))
        self.assertTrue(_is_critical('Potassium', 7.0, 'mEq/L', True))
        self.assertFalse(_is_critical('Potassium', 4.2, 'mEq/L', True))


class IngestionPathParityTests(TestCase):
    """
    All three paths must classify identically. Previously only Kanta computed
    criticality at all, using the raw value.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='cb1-parity', password='pw-test-only', email='cb1@example.com')

    def _persist(self, name, value, unit):
        record = MedicalRecord.objects.create(
            patient=self.user, title='t', record_type='lab_result')
        _save_lab_value(record, {'name': name, 'value': value, 'unit': unit})
        return ParsedLabValue.objects.get(record=record)

    def test_save_lab_value_sets_criticality_without_being_told(self):
        """
        The single point every path funnels through. `_save_lab_value` no longer
        accepts an is_critical argument — that parameter is how the paths
        diverged.
        """
        row = self._persist('Glucose', '500', 'mg/dL')
        self.assertTrue(row.is_critical)
        self.assertTrue(row.is_abnormal)

    def test_benign_us_glucose_is_not_flagged_through_persistence(self):
        row = self._persist('Glucose', '140', 'mg/dL')
        self.assertFalse(row.is_critical)

    def test_si_input_is_normalised_then_judged(self):
        """A Finnish record: 5.0 mmol/L stored as 90.08 mg/dL, not critical."""
        row = self._persist('Glucose', '5.0', 'mmol/L')
        self.assertAlmostEqual(row.canonical_value, 90.08, places=2)
        self.assertEqual(row.unit, 'mg/dL')
        self.assertFalse(row.is_critical)

    def test_si_critical_input_is_detected_after_normalisation(self):
        row = self._persist('Glucose', '30', 'mmol/L')
        self.assertTrue(row.is_critical)

    def test_identical_measurement_in_both_units_agrees(self):
        si = self._persist('Glucose', '30', 'mmol/L')
        us = self._persist('Glucose', str(30 * 18.016), 'mg/dL')
        self.assertEqual(si.is_critical, us.is_critical)

    def test_unresolvable_unit_is_persisted_but_not_critical(self):
        row = self._persist('Glucose', '140', 'furlongs')
        self.assertFalse(row.is_critical)
        self.assertFalse(row.unit_known)

    def test_save_lab_value_rejects_a_precomputed_flag(self):
        """
        Pinning the API change: accepting is_critical from callers is what let
        the Kanta path use a raw comparison. It must not come back.
        """
        record = MedicalRecord.objects.create(
            patient=self.user, title='t', record_type='lab_result')
        with self.assertRaises(TypeError):
            _save_lab_value(record, {'name': 'Glucose', 'value': '5', 'unit': 'mmol/L'},
                            is_critical=True)

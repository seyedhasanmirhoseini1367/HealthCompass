from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.medical_records.models import MedicalRecord, ParsedLabValue
from apps.medical_records.parsers import (
    _parse_date_string, _extract_date_regex, _extract_lab_values_regex, WearableParser,
)
from apps.medical_records.services import _save_lab_value


class ParseDateStringTest(TestCase):
    """Unit tests for the unified _parse_date_string function."""

    # ── Layer 1: unambiguous formats ─────────────────────────────────────────

    def test_iso_hyphen(self):
        self.assertEqual(_parse_date_string('2024-11-15'), '2024-11-15')

    def test_iso_slash(self):
        self.assertEqual(_parse_date_string('2024/11/15'), '2024-11-15')

    def test_iso_dot(self):
        self.assertEqual(_parse_date_string('2024.11.15'), '2024-11-15')

    def test_alpha_month_long(self):
        self.assertEqual(_parse_date_string('November 15, 2024'), '2024-11-15')

    def test_alpha_month_short(self):
        self.assertEqual(_parse_date_string('Nov 15 2024'), '2024-11-15')

    def test_alpha_month_reversed(self):
        self.assertEqual(_parse_date_string('15 November 2024'), '2024-11-15')

    # ── Layer 2: structural disambiguation (one component > 12) ──────────────

    def test_day_first_gt12_forces_eu(self):
        # 25 can only be day; 04 must be month → April 25
        self.assertEqual(_parse_date_string('25/04/2024'), '2024-04-25')

    def test_second_gt12_forces_us_style(self):
        # 04 can be month; 25 must be day → April 25
        self.assertEqual(_parse_date_string('04/25/2024'), '2024-04-25')

    def test_dot_separator_gt12(self):
        self.assertEqual(_parse_date_string('31.01.2024'), '2024-01-31')

    def test_impossible_both_gt12_returns_none(self):
        self.assertIsNone(_parse_date_string('25/25/2024'))

    # ── Layer 3: genuinely ambiguous, locale_hint ─────────────────────────────

    def test_ambiguous_eu_explicit(self):
        # 03/04/2024 with EU → 3 April (day=03, month=04)
        self.assertEqual(_parse_date_string('03/04/2024', locale_hint='eu'), '2024-04-03')

    def test_ambiguous_us_explicit(self):
        # 03/04/2024 with US → 4 March (month=03, day=04)
        self.assertEqual(_parse_date_string('03/04/2024', locale_hint='us'), '2024-03-04')

    @override_settings(DATE_FORMAT_PREFERENCE='eu')
    def test_ambiguous_falls_back_to_eu_setting(self):
        self.assertEqual(_parse_date_string('03/04/2024'), '2024-04-03')

    @override_settings(DATE_FORMAT_PREFERENCE='us')
    def test_ambiguous_falls_back_to_us_setting(self):
        self.assertEqual(_parse_date_string('03/04/2024'), '2024-03-04')

    def test_invalid_date_returns_none(self):
        self.assertIsNone(_parse_date_string('99/99/2024'))

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_date_string(''))

    def test_two_digit_year_expanded(self):
        self.assertEqual(_parse_date_string('25/04/24'), '2024-04-25')


class ExtractDateRegexTest(TestCase):
    """_extract_date_regex must find and correctly parse dates from free text."""

    def test_labelled_iso(self):
        self.assertEqual(_extract_date_regex('Date: 2024-11-15'), '2024-11-15')

    def test_labelled_alpha(self):
        self.assertEqual(
            _extract_date_regex('Collection Date: November 15, 2024'), '2024-11-15'
        )

    def test_labelled_ambiguous_eu(self):
        # Kanta/EU context: 03/04/2024 should be 3 April
        self.assertEqual(
            _extract_date_regex('Test Date: 03/04/2024', locale_hint='eu'), '2024-04-03'
        )

    def test_labelled_ambiguous_us(self):
        # US context: 03/04/2024 should be 4 March
        self.assertEqual(
            _extract_date_regex('Test Date: 03/04/2024', locale_hint='us'), '2024-03-04'
        )

    def test_unlabelled_long_month(self):
        self.assertEqual(
            _extract_date_regex('Patient seen on November 15, 2024 for follow-up'), '2024-11-15'
        )

    def test_reversed_month(self):
        self.assertEqual(
            _extract_date_regex('Admitted 15 November 2024'), '2024-11-15'
        )

    def test_iso_in_body(self):
        self.assertEqual(
            _extract_date_regex('Results from 2024-03-01 are reviewed.'), '2024-03-01'
        )

    def test_returns_none_on_no_date(self):
        self.assertIsNone(_extract_date_regex('No date information here.'))


class WearableParserDateTest(TestCase):
    """WearableParser._parse_dt must use the same disambiguation as _parse_date_string."""

    def setUp(self):
        self.parser = WearableParser()

    def test_iso_datetime(self):
        dt = self.parser._parse_dt('2024-11-15 08:30:00')
        self.assertEqual(dt.strftime('%Y-%m-%d'), '2024-11-15')

    def test_iso_date_only(self):
        dt = self.parser._parse_dt('2024-11-15')
        self.assertEqual(dt.strftime('%Y-%m-%d'), '2024-11-15')

    def test_structural_disambiguation_with_time(self):
        # 25/04 → day must be 25 regardless of locale
        dt = self.parser._parse_dt('25/04/2024 08:30')
        self.assertEqual(dt.strftime('%Y-%m-%d'), '2024-04-25')

    def test_ambiguous_eu(self):
        dt = self.parser._parse_dt('03/04/2024', locale_hint='eu')
        self.assertEqual(dt.strftime('%Y-%m-%d'), '2024-04-03')

    def test_ambiguous_us(self):
        dt = self.parser._parse_dt('03/04/2024', locale_hint='us')
        self.assertEqual(dt.strftime('%Y-%m-%d'), '2024-03-04')

    @override_settings(DATE_FORMAT_PREFERENCE='eu')
    def test_uses_settings_default(self):
        dt = self.parser._parse_dt('03/04/2024')
        self.assertEqual(dt.strftime('%Y-%m-%d'), '2024-04-03')

    def test_same_convention_as_extract_date_regex(self):
        """Both parsers must produce the same result for the same input and locale."""
        for locale in ('eu', 'us'):
            with self.subTest(locale=locale):
                dt = self.parser._parse_dt('03/04/2024', locale_hint=locale)
                iso = _parse_date_string('03/04/2024', locale_hint=locale)
                self.assertEqual(dt.strftime('%Y-%m-%d'), iso)


class ParserNormalizerChainTest(TestCase):
    """
    Integration: FIMLAB text → _extract_lab_values_regex → _save_lab_value
                 → ParsedLabValue with correct canonical_value and unit_known.

    Covers the parser→normalizer→model chain end-to-end so that a broken link
    (e.g. regex not capturing µmol/L) is caught before it silently drops values
    from trajectory comparisons.
    """

    # Realistic FIMLAB lab report snippets (Finnish SI units)
    _PANEL = (
        "Hemoglobin          145    g/L      117-155\n"
        "Creatinine          112    µmol/L   45-90\n"    # U+00B5 MICRO SIGN
        "Glucose             5.8    mmol/L   3.9-6.1\n"
        "Bilirubin           12     µmol/L   2-21\n"
    )

    def setUp(self):
        user = get_user_model().objects.create_user(username='chaintest', password='pw')
        self.record = MedicalRecord.objects.create(
            patient=user, title='FIMLAB Chain Test', record_type='lab_result',
        )

    def _run_chain(self, text):
        """Parse text and persist each extracted lab value; return queryset of results."""
        for lv in _extract_lab_values_regex(text):
            _save_lab_value(self.record, lv)
        return ParsedLabValue.objects.filter(record=self.record)

    # ── Individual unit tests ─────────────────────────────────────────────────

    def test_g_per_L_hemoglobin_captured_and_converted(self):
        """g/L is ASCII-clean; must be captured and normalised to g/dL."""
        rows = self._run_chain("Hemoglobin  145  g/L  117-155")
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertTrue(row.unit_known, "g/L must be a known unit")
        self.assertAlmostEqual(row.canonical_value, 14.5, places=2)   # 145 g/L ÷ 10
        self.assertEqual(row.unit, 'g/dL')
        self.assertEqual(row.original_unit, 'g/L')

    def test_micro_sign_umol_creatinine_captured_and_converted(self):
        """µmol/L (U+00B5 MICRO SIGN) must be captured by the regex and converted."""
        rows = self._run_chain("Creatinine  112  µmol/L  45-90")
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertTrue(
            row.unit_known,
            "µmol/L (U+00B5) was not captured — regex first-char class missing µ"
        )
        # 112 µmol/L ÷ 88.4 = 1.2670 mg/dL
        self.assertAlmostEqual(row.canonical_value, round(112 / 88.4, 4), places=4)
        self.assertEqual(row.unit, 'mg/dL')

    def test_mmol_L_glucose_captured_and_converted(self):
        """mmol/L (ASCII) must be captured and converted."""
        rows = self._run_chain("Glucose  5.8  mmol/L  3.9-6.1")
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertTrue(row.unit_known)
        self.assertAlmostEqual(row.canonical_value, round(5.8 * 18.016, 4), places=3)
        self.assertEqual(row.unit, 'mg/dL')

    def test_micro_sign_umol_bilirubin_captured_and_converted(self):
        """µmol/L bilirubin (U+00B5) must be captured and converted."""
        rows = self._run_chain("Bilirubin  12  µmol/L  2-21")
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertTrue(row.unit_known)
        self.assertAlmostEqual(row.canonical_value, round(12 / 17.1, 4), places=4)
        self.assertEqual(row.unit, 'mg/dL')

    # ── Safety net: missing unit ──────────────────────────────────────────────

    def test_missing_unit_known_analyte_sets_unit_known_false(self):
        """
        If the unit is absent for a known analyte, unit_known must be False.
        This is the safety net that keeps the value out of trajectory comparisons
        rather than silently treating the raw number as canonical.
        """
        lab_values = _extract_lab_values_regex("Hemoglobin  145  117-155")
        self.assertTrue(lab_values, "Regex must extract hemoglobin even without explicit unit")
        hgb = next((lv for lv in lab_values if 'hemoglobin' in lv['name'].lower()), None)
        self.assertIsNotNone(hgb, "Hemoglobin must appear in extracted values")
        self.assertEqual(hgb['unit'], '', "Unit field must be empty string when not captured")

        _save_lab_value(self.record, hgb)
        row = ParsedLabValue.objects.get(record=self.record)
        self.assertFalse(
            row.unit_known,
            "Known analyte without unit must yield unit_known=False "
            "(excluded from trajectory, not silently misinterpreted)"
        )

    # ── Full FIMLAB panel ─────────────────────────────────────────────────────

    def test_full_fimlab_panel_all_units_known(self):
        """
        A complete FIMLAB-style panel (4 analytes, mixed ASCII + µ units) must
        produce unit_known=True for every row after the fix.
        Any unit_known=False here means a unit wasn't captured — a silent chain break.
        """
        rows = self._run_chain(self._PANEL)
        self.assertEqual(rows.count(), 4, "All 4 analytes must produce a ParsedLabValue row")

        unknown = rows.filter(unit_known=False)
        if unknown.exists():
            names = list(unknown.values_list('parameter_name', flat=True))
            self.fail(
                f"unit_known=False for: {names}. "
                "These values will be silently excluded from trajectory."
            )

    def test_full_fimlab_panel_canonical_values_correct(self):
        """Spot-check canonical values for the full panel."""
        self._run_chain(self._PANEL)
        rows = {r.parameter_name.lower(): r
                for r in ParsedLabValue.objects.filter(record=self.record)}

        # Hemoglobin: 145 g/L → 14.5 g/dL
        hgb = next((v for k, v in rows.items() if 'hemoglobin' in k), None)
        self.assertIsNotNone(hgb)
        self.assertAlmostEqual(hgb.canonical_value, 14.5, places=2)

        # Creatinine: 112 µmol/L → 1.267 mg/dL
        cre = next((v for k, v in rows.items() if 'creatinine' in k), None)
        self.assertIsNotNone(cre)
        self.assertAlmostEqual(cre.canonical_value, round(112 / 88.4, 4), places=4)

        # Glucose: 5.8 mmol/L → ~104.49 mg/dL
        glu = next((v for k, v in rows.items() if 'glucose' in k), None)
        self.assertIsNotNone(glu)
        self.assertAlmostEqual(glu.canonical_value, round(5.8 * 18.016, 4), places=2)

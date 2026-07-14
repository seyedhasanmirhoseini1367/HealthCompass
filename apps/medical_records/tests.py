from django.test import TestCase, override_settings

from apps.medical_records.parsers import _parse_date_string, _extract_date_regex, WearableParser


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

"""Regression tests for audit SEC-01: stored XSS through lab parameter names.

A Kanta displayName was stored verbatim and later written into the population
page with innerHTML, so a patient could run script in a staff member's browser.
"""
import pathlib
import re
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.medical_records.models import ParsedLabValue
from apps.medical_records.services import MedicalRecordService, normalise_parameter_name

PAYLOAD = '<img src=x onerror=alert(1)>'


class NormaliseParameterNameTests(SimpleTestCase):
    def test_strips_angle_brackets(self):
        self.assertNotRegex(normalise_parameter_name(PAYLOAD), '[<>]')

    def test_strips_control_characters_and_collapses_whitespace(self):
        self.assertEqual(normalise_parameter_name('B-\x00Hb\n\t  total'), 'B- Hb total')

    def test_caps_length(self):
        self.assertEqual(len(normalise_parameter_name('x' * 500)), 120)

    def test_empty_becomes_unknown(self):
        self.assertEqual(normalise_parameter_name(' <> '), 'Unknown')
        self.assertEqual(normalise_parameter_name(None), 'Unknown')

    def test_ordinary_name_unchanged(self):
        self.assertEqual(normalise_parameter_name('P-Krea'), 'P-Krea')


class KantaIngestionSanitisesNamesTests(TestCase):
    def test_kanta_display_name_is_stored_without_markup(self):
        user = get_user_model().objects.create_user(
            username='xss_patient', email='xss@example.com', password='x-Strong-pass-91')
        parsed = {'records': [{
            'type': 'lab', 'title': 'Lab', 'date': '2026-01-01',
            'sections': [{'entries': [{
                'kind': 'lab', 'name': PAYLOAD, 'value': '5.1', 'unit': 'mmol/L<b>',
            }]}],
        }]}
        with patch('apps.medical_records.parsers.KantaXMLParser.parse', return_value=parsed):
            result = MedicalRecordService.create_from_kanta(user, b'<x/>')
        self.assertEqual(result.get('lab_values_created'), 1)
        row = ParsedLabValue.objects.get(patient=user)
        self.assertNotRegex(row.parameter_name, '[<>]')
        self.assertNotRegex(row.unit + row.original_unit, '[<>]')


class TemplatesDoNotInterpolateIntoInnerHtmlTests(SimpleTestCase):
    """The two biomarker browsers must build rows with the DOM, not innerHTML."""

    TEMPLATES = ['ai_insights/population.html', 'ai_insights/health.html']

    def test_no_template_literal_reaches_inner_html(self):
        root = pathlib.Path(settings.BASE_DIR) / 'templates'
        for name in self.TEMPLATES:
            source = (root / name).read_text(encoding='utf-8')
            with self.subTest(template=name):
                self.assertNotIn('<span>${bio}</span>', source)
                self.assertIsNone(re.search(r'innerHTML\s*=\s*`[^`]*\$\{', source),
                                  'template literal with interpolation assigned to innerHTML')

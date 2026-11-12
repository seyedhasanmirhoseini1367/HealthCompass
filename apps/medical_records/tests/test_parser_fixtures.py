"""Table-driven parser tests over small fixture files (prompt 9 task 2)."""
from pathlib import Path

from django.test import SimpleTestCase

from apps.medical_records.parsers import KantaXMLParser, WearableParser

FIXTURES = Path(__file__).parent / 'fixtures'

KANTA_LAB = b"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <title>Laboratoriotutkimukset</title>
  <effectiveTime value="20260115"/>
  <component><structuredBody><component><section>
    <code code="LAB" displayName="Laboratoriotulokset"/>
    <entry><observation>
      <code displayName="B-Hb"/>
      <effectiveTime value="20260115"/>
      <value value="140" unit="g/l"/>
    </observation></entry>
  </section></component></structuredBody></component>
</ClinicalDocument>"""


class WearableFixtureTests(SimpleTestCase):
    CASES = [
        # (fixture, expect points?)
        ('generic_steps.csv', True),
        ('fitbit_style.csv', True),
        ('simple.json', True),
        ('unknown.csv', False),
        ('binary.csv', False),
    ]

    def test_each_fixture(self):
        for name, expect_points in self.CASES:
            with self.subTest(fixture=name):
                result = WearableParser().parse((FIXTURES / name).read_bytes(), filename=name)
                self.assertIsInstance(result, dict)
                self.assertEqual(bool(result.get('data_points')), expect_points, result.get('errors'))


class KantaParserTests(SimpleTestCase):
    def test_a_minimal_document_parses(self):
        result = KantaXMLParser().parse(KANTA_LAB)
        self.assertNotIn('error', result)
        self.assertTrue(result.get('records'))

    def test_malformed_xml_is_an_error_not_an_exception(self):
        for bad in (b'', b'<not-closed>', b'\xff\xfe garbage'):
            with self.subTest(bad=bad[:12]):
                self.assertIn('error', KantaXMLParser().parse(bad))

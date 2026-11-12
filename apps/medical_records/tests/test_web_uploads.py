"""Request-level tests for the web upload views (audit T-07, prompt 9 task 2):
success, wrong type, oversize, AJAX and non-AJAX, and refused consent."""
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.medical_records.models import MedicalRecord
from apps.testing.factories import make_patient

AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
PDF = b'%PDF-1.4 ' + b'x' * 64
KANTA = (b'<?xml version="1.0"?><ClinicalDocument xmlns="urn:hl7-org:v3">'
         b'<title>Lab</title></ClinicalDocument>')
CSV = b'date,steps,heart_rate\n2026-01-01,1000,60\n2026-01-02,2000,62\n'


class _Uploads(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_patient()
        self.client.force_login(self.user)

    def count(self):
        return MedicalRecord.objects.filter(patient=self.user).count()


class PdfUploadTests(_Uploads):
    URL = '/records/upload/pdf/'

    def _post(self, content=PDF, name='lab.pdf', **extra):
        with patch('apps.medical_records.parsers.PDFParser.parse',
                   return_value={'raw_text': 'Hb 140 g/L', 'page_count': 1, 'structured': None}):
            return self.client.post(self.URL, {'pdf_file': SimpleUploadedFile(name, content),
                                               'record_type': 'lab_result'}, **extra)

    def test_success_ajax_returns_the_record(self):
        response = self._post(**AJAX)
        self.assertTrue(response.json()['success'])
        self.assertEqual(self.count(), 1)

    def test_success_non_ajax_redirects_to_the_record(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/records/', response['Location'])

    def test_wrong_type_is_refused(self):
        response = self._post(content=b'<html></html>', name='page.html', **AJAX)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.count(), 0)

    def test_a_fake_pdf_is_refused_by_magic_bytes(self):
        self.assertEqual(self._post(content=b'not a pdf', **AJAX).status_code, 400)

    def test_oversize_is_refused(self):
        with override_settings(MAX_UPLOAD_BYTES=10):
            response = self._post(**AJAX)
        self.assertEqual(response.status_code, 400)
        self.assertIn('too large', response.json()['error'])


class TextUploadTests(_Uploads):
    URL = '/records/upload/text/'

    def test_success_and_ajax_shape(self):
        response = self.client.post(self.URL, {'text': 'Hb 140 g/L', 'record_type': 'auto'}, **AJAX)
        self.assertTrue(response.json()['success'])
        self.assertIn('processing_status', response.json()['record'])

    def test_empty_text_is_refused(self):
        response = self.client.post(self.URL, {'text': '', 'record_type': 'auto'}, **AJAX)
        self.assertEqual(response.status_code, 400)

    def test_without_consent_the_record_is_saved_but_not_ai_processed(self):
        with patch('apps.medical_records.parsers._structure_medical_text_with_ai',
                   return_value=None) as ai:
            self.client.post(self.URL, {'text': 'Hb 140 g/L', 'record_type': 'auto'}, **AJAX)
        self.assertEqual(self.count(), 1)
        self.assertFalse(ai.call_args.kwargs.get('allow_external', True) if ai.called else False)


class KantaUploadTests(_Uploads):
    URL = '/records/upload/kanta/'

    def test_success(self):
        with patch('apps.medical_records.services.MedicalRecordService.create_from_kanta',
                   return_value={'records_created': 2, 'records_skipped': 0, 'lab_values_created': 3,
                                 'record_ids': [], 'flagged': 0}):
            response = self.client.post(self.URL, {'xml_file': SimpleUploadedFile('k.xml', KANTA)}, **AJAX)
        self.assertLess(response.status_code, 400)

    def test_wrong_type(self):
        response = self.client.post(self.URL, {'xml_file': SimpleUploadedFile('k.pdf', PDF)}, **AJAX)
        self.assertGreaterEqual(response.status_code, 400)

    def test_hostile_xml_is_rejected(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
                b'<x>&b;</x>')
        response = self.client.post(self.URL, {'xml_file': SimpleUploadedFile('k.xml', bomb)}, **AJAX)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(self.count(), 0)


class WearableUploadTests(_Uploads):
    URL = '/records/upload/wearable/'

    def test_csv_success(self):
        response = self.client.post(self.URL, {'data_file': SimpleUploadedFile('w.csv', CSV)}, **AJAX)
        self.assertLess(response.status_code, 400, response.content[:200])

    def test_xml_is_refused_as_advertised_by_the_picker(self):
        response = self.client.post(self.URL, {'data_file': SimpleUploadedFile('w.xml', KANTA)}, **AJAX)
        self.assertGreaterEqual(response.status_code, 400)


class ScanUploadTests(_Uploads):
    def test_ocr_refused_without_consent(self):
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 64
        with patch('apps.medical_records.services.MedicalRecordService.ocr_image',
                   return_value={'error': 'Consent needed', 'consent_required': 'external_llm'}):
            response = self.client.post('/records/upload/scan/ocr/',
                                        {'image': SimpleUploadedFile('s.png', png)}, **AJAX)
        self.assertGreaterEqual(response.status_code, 400)

    def test_scan_text_is_saved(self):
        response = self.client.post('/records/upload/scan/', {'raw_text': 'Hb 140', 'record_type': 'lab_result'},
                                    **AJAX)
        self.assertLess(response.status_code, 400)
        self.assertEqual(self.count(), 1)


class AnonymousUploadTests(TestCase):
    def test_every_upload_url_needs_login(self):
        for url in ('/records/upload/pdf/', '/records/upload/text/', '/records/upload/kanta/',
                    '/records/upload/wearable/', '/records/upload/scan/', reverse('medical_records:upload_any')):
            with self.subTest(url=url):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login', response['Location'])

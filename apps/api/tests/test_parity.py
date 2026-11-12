"""Web and API apply the same rules (audit CQ-02, prompt 4), plus the API
conventions: pagination, list shape, corrected lab values, error body, schema."""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import SharingGrant
from apps.medical_records.models import MedicalRecord, ParsedLabValue

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('par', email='par@test.invalid', password='pw')
        self.client.force_login(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)


class UploadParityTests(_Base):
    BAD = [('page.html', b'<html>x</html>'), ('tool.exe', b'MZ\x90\x00'),
           ('fake.pdf', b'not a pdf at all')]

    def test_bad_files_are_refused_by_web_and_api(self):
        for name, content in self.BAD:
            with self.subTest(file=name, surface='api-generic'):
                r = self.api.post('/api/v1/records/upload/',
                                  {'file': SimpleUploadedFile(name, content)}, format='multipart')
                self.assertEqual(r.status_code, 400)
            with self.subTest(file=name, surface='web-pdf'):
                self.client.post('/records/upload/pdf/', {'pdf_file': SimpleUploadedFile(name, content)},
                                 HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertFalse(MedicalRecord.objects.filter(patient=self.user).exists())

    def test_wearable_accept_attribute_matches_what_is_accepted(self):
        from apps.medical_records.forms import WearableUploadForm
        from apps.medical_records.services import ALLOWED_EXTENSIONS
        accept = WearableUploadForm().fields['data_file'].widget.attrs['accept']
        self.assertEqual(accept.split(','), [f'.{e}' for e in ALLOWED_EXTENSIONS['wearable']])
        self.assertNotIn('.xml', accept)


class ShareParityTests(_Base):
    def test_bad_share_input_is_refused_by_both(self):
        for payload in ({'scopes': ['records']}, {'recipient': 'x@test.invalid', 'scopes': []},
                        {'recipient': 'par@test.invalid', 'scopes': ['records']}):
            with self.subTest(payload=payload):
                self.client.post('/accounts/my-shares/create/', payload)
                api_payload = {'identifier': payload.get('recipient', ''), 'scopes': payload['scopes']}
                r = self.api.post('/api/v1/sharing/companions/create/', api_payload, format='json')
                self.assertEqual(r.status_code, 400)
        self.assertFalse(SharingGrant.objects.exists())


class ProfileParityTests(_Base):
    def test_bad_phone_is_refused_by_both(self):
        from apps.accounts.forms import ProfileForm
        form = ProfileForm(data={'phone_number': 'call me maybe'}, instance=self.user)
        self.assertFalse(form.is_valid())
        r = self.api.patch('/api/v1/auth/profile/', {'phone_number': 'call me maybe'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_bad_blood_type_is_refused_by_both(self):
        from apps.accounts.forms import PatientProfileForm
        self.assertFalse(PatientProfileForm(data={'blood_type': 'Q'}).is_valid())
        r = self.api.patch('/api/v1/auth/emergency-card/', {'blood_type': 'Q'}, format='json')
        self.assertEqual(r.status_code, 400)


class PaginationTests(_Base):
    def test_records_are_paged_with_headers_and_no_parsed_data(self):
        for i in range(25):
            MedicalRecord.objects.create(patient=self.user, title=f'r{i}', parsed_data={'x': i})
        first = self.api.get('/api/v1/records/')
        self.assertEqual(len(first.json()), 20)
        self.assertEqual(first['X-Total-Count'], '25')
        self.assertIn('rel="next"', first['Link'])
        self.assertNotIn('parsed_data', first.json()[0])
        second = self.api.get('/api/v1/records/?page=2')
        self.assertEqual(len(second.json()), 5)
        self.assertEqual(len(self.api.get('/api/v1/records/?page_size=500').json()), 25)

    def test_an_out_of_range_page_is_empty_not_an_error(self):
        self.assertEqual(self.api.get('/api/v1/records/?page=99').json(), [])

    def test_sessions_keep_their_shape_and_gain_a_count(self):
        body = self.api.get('/api/v1/assistant/sessions/').json()
        self.assertIn('sessions', body)
        self.assertIn('count', body)


class CorrectedLabValueTests(_Base):
    def test_api_returns_the_corrected_reading(self):
        from apps.medical_records.models import LabValueCorrection
        record = MedicalRecord.objects.create(patient=self.user, title='lab')
        lv = ParsedLabValue.objects.create(record=record, parameter_name='Hb', value='14', unit='g/L')
        LabValueCorrection.objects.create(original=lv, value='140', unit='g/L', reason='misread')
        body = self.api.get(f'/api/v1/records/{record.pk}/').json()
        row = body['lab_values'][0]
        self.assertEqual(row['value'], '140')
        self.assertTrue(row['is_corrected'])


class ErrorBodyTests(_Base):
    def test_errors_have_error_and_code(self):
        body = APIClient().get('/api/v1/records/').json()
        self.assertIn('error', body)
        self.assertIn('code', body)


class SchemaTests(TestCase):
    def test_schema_generates_with_operation_ids(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'schema.yaml')
            call_command('spectacular', '--file', path, '--validate')
            text = open(path, encoding='utf-8').read()
        for op in ('operationId: login', 'operationId: records_list', 'operationId: create_share'):
            self.assertIn(op, text)

    def test_docs_are_staff_only(self):
        user = User.objects.create_user('nostaff', email='ns@test.invalid', password='pw')
        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.get('/api/v1/schema/').status_code, 403)
        api.force_authenticate(User.objects.create_superuser('st', email='st@test.invalid', password='pw'))
        self.assertEqual(api.get('/api/v1/schema/').status_code, 200)

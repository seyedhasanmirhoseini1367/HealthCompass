"""
GDPR data export tests.

Every assertion opens the produced ZIP and reads what is actually inside it.
Testing the HTTP status alone would pass just as happily for an archive that
omitted half the user's data or included somebody else's — which are precisely
the two failures that matter here.
"""
import io
import json
import zipfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.consent import grant_consent, revoke_consent
from apps.accounts.export import EXPORT_VERSION, build_export
from apps.accounts.models import (Consent, ConsentPurpose, DoctorAccessLog,
                                  PatientDoctorRelationship, PatientProfile)
from apps.ai_insights.models import AIModel, HealthAlert, ModelPrediction
from apps.appointments.models import Appointment
from apps.medical_records.models import (MedicalRecord, ParsedLabValue,
                                         WearableDataPoint)
from apps.notifications.models import FCMDevice, Notification
from apps.rag_assistant.models import ChatSession, MedicalChunk, MedicalDocument, QueryLog

User = get_user_model()
EXTERNAL = ConsentPurpose.EXTERNAL_LLM

# Indexing on save would call the embedding provider; irrelevant here.
NO_AUTOINDEX = override_settings(RAG_AUTO_INDEX_SYNC=False)


def open_archive(user):
    """Build the export and return (ZipFile, name-set)."""
    buffer, filename = build_export(user)
    zf = zipfile.ZipFile(buffer)
    return zf, set(zf.namelist()), filename


def read_json(zf, name):
    return json.loads(zf.read(name).decode('utf-8'))


class _FixtureMixin:
    """A user with at least one row in every exportable category."""

    def build_populated_user(self, username='subject', *, with_files=True):
        user = User.objects.create_user(
            username=username, email=f'{username}@example.com',
            password='pw-export-1', first_name='Ex', last_name='Ample',
            phone_number='+358401234567',
        )
        PatientProfile.objects.create(
            user=user, blood_type='O+', allergies='Penicillin',
            emergency_contact_name='Kin', emergency_contact_phone='+358401111111',
            national_id='010190-123A',
        )

        record = MedicalRecord.objects.create(
            patient=user, title=f'{username} Bloodwork', record_type='lab_result',
            raw_text='CREATININE 250 umol/L', parsed_data={'lab_values': [{'name': 'CREATININE'}]},
            record_date=timezone.now().date(),
            file=SimpleUploadedFile(f'{username}-labs.pdf', b'%PDF-1.4 fake') if with_files else None,
        )
        ParsedLabValue.objects.create(
            record=record, parameter_name='CREATININE', value='250', unit='umol/L',
            canonical_value=2.83, original_unit='umol/L', is_abnormal=True,
        )
        WearableDataPoint.objects.create(
            record=record, metric='heart_rate', value=72.0, unit='bpm',
            recorded_at=timezone.now(),
        )

        session = ChatSession.objects.create(patient=user, title=f'{username} chat')
        QueryLog.objects.create(
            session=session, query=f'{username} question about creatinine',
            response=f'{username} answer', sources=[{'title': 'Lab'}],
            llm_provider='groq',
        )

        doc = MedicalDocument.objects.create(
            patient=user, title=f'{username} doc', document_type='lab_result',
            content='CREATININE 250', record=record,
        )
        MedicalChunk.objects.create(
            document=doc, patient=user, content=f'{username} chunk text', chunk_index=0,
            embedding=b'\x00' * 16, embedding_model='models/gemini-embedding-001',
            embedding_dimensions=3072,
        )

        Appointment.objects.create(
            patient=user, title=f'{username} consult',
            appointment_datetime=timezone.now() + timedelta(days=5),
        )
        HealthAlert.objects.create(
            patient=user, severity='critical', title=f'{username} alert',
            message='High potassium', source_record=record,
        )
        ai_model = AIModel.objects.create(
            data_scientist=user, name=f'{username} Model', description='d', status='active',
        )
        ModelPrediction.objects.create(
            model=ai_model, patient=user, input_data={'age': 61},
            result={'label': f'{username} high risk'}, risk_score=0.9,
            input_file=SimpleUploadedFile(f'{username}-input.csv', b'a,b\n1,2') if with_files else None,
        )
        Notification.objects.create(user=user, title=f'{username} notification', message='m')
        FCMDevice.objects.create(user=user, token=f'{username}-secret-device-token')
        grant_consent(user, EXTERNAL)
        return user, record


@NO_AUTOINDEX
class ExportCompletenessTests(_FixtureMixin, TestCase):

    def setUp(self):
        cache.clear()
        self.user, self.record = self.build_populated_user()

    def test_archive_contains_every_expected_file(self):
        _zf, names, _fn = open_archive(self.user)
        for expected in ('manifest.json', 'user.json', 'medical_records.json',
                         'conversations.json', 'appointments.json', 'insights.json',
                         'predictions.json', 'consent.json', 'notifications.json',
                         'access_history.json', 'rag_index.json'):
            self.assertIn(expected, names)

    def test_manifest_declares_version_timestamp_and_categories(self):
        zf, _names, _fn = open_archive(self.user)
        manifest = read_json(zf, 'manifest.json')
        self.assertEqual(manifest['export_version'], EXPORT_VERSION)
        self.assertTrue(manifest['generated_at'])
        self.assertEqual(manifest['subject']['username'], 'subject')
        categories = {c['category'] for c in manifest['data_categories']}
        self.assertIn('medical_records', categories)
        self.assertIn('consent', categories)
        self.assertTrue(manifest['exclusions'])

    def test_manifest_does_not_leak_the_internal_user_id(self):
        zf, _names, _fn = open_archive(self.user)
        manifest = read_json(zf, 'manifest.json')
        self.assertNotIn('id', manifest['subject'])
        self.assertNotIn('user_id', manifest['subject'])

    def test_medical_records_include_children_and_file_reference(self):
        zf, _names, _fn = open_archive(self.user)
        records = read_json(zf, 'medical_records.json')
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec['title'], 'subject Bloodwork')
        self.assertEqual(rec['raw_text'], 'CREATININE 250 umol/L')
        self.assertEqual(len(rec['lab_values']), 1)
        self.assertEqual(rec['lab_values'][0]['parameter_name'], 'CREATININE')
        self.assertEqual(len(rec['wearable_points']), 1)
        self.assertTrue(rec['attached_file'].startswith('files/medical_records/'))

    def test_conversations_include_questions_and_answers(self):
        zf, _names, _fn = open_archive(self.user)
        convos = read_json(zf, 'conversations.json')
        self.assertEqual(len(convos), 1)
        self.assertEqual(len(convos[0]['messages']), 1)
        self.assertIn('creatinine', convos[0]['messages'][0]['question'])

    def test_appointments_insights_predictions_present(self):
        zf, _names, _fn = open_archive(self.user)
        self.assertEqual(len(read_json(zf, 'appointments.json')), 1)
        insights = read_json(zf, 'insights.json')
        self.assertEqual(len(insights['health_alerts']), 1)
        self.assertEqual(len(insights['submitted_ai_models']), 1)
        predictions = read_json(zf, 'predictions.json')
        self.assertEqual(predictions[0]['result']['label'], 'subject high risk')

    def test_consent_history_is_included(self):
        revoke_consent(self.user, EXTERNAL)
        zf, _names, _fn = open_archive(self.user)
        consent = read_json(zf, 'consent.json')
        self.assertTrue(consent['history'])
        entry = next(h for h in consent['history'] if h['purpose'] == EXTERNAL)
        self.assertEqual(entry['status'], Consent.Status.REVOKED)
        self.assertTrue(entry['granted_at'])
        self.assertTrue(entry['revoked_at'])
        self.assertTrue(consent['current'])

    def test_rag_index_includes_chunk_text_but_not_vectors(self):
        zf, _names, _fn = open_archive(self.user)
        rag = read_json(zf, 'rag_index.json')
        self.assertEqual(len(rag['documents']), 1)
        chunk = rag['chunks'][0]
        self.assertEqual(chunk['content'], 'subject chunk text')
        self.assertEqual(chunk['embedding']['model'], 'models/gemini-embedding-001')
        self.assertEqual(chunk['embedding']['vector'], 'excluded — see manifest')

    def test_account_includes_profile_and_own_national_id(self):
        zf, _names, _fn = open_archive(self.user)
        account = read_json(zf, 'user.json')
        self.assertEqual(account['email'], 'subject@example.com')
        self.assertEqual(account['patient_profile']['blood_type'], 'O+')
        # The subject's own identifier is their personal data and is in scope.
        self.assertEqual(account['patient_profile']['national_id'], '010190-123A')


@NO_AUTOINDEX
class ExportSensitiveDataTests(_FixtureMixin, TestCase):
    """Credentials and secrets must never reach the archive."""

    def setUp(self):
        cache.clear()
        self.user, _ = self.build_populated_user()

    def _all_text(self):
        zf, names, _fn = open_archive(self.user)
        return '\n'.join(
            zf.read(n).decode('utf-8', errors='replace')
            for n in names if n.endswith('.json')
        )

    def test_password_hash_is_absent(self):
        blob = self._all_text()
        self.assertNotIn(self.user.password, blob)
        self.assertNotIn('pbkdf2', blob.lower())
        self.assertNotIn('"password"', blob.lower())

    def test_push_device_token_is_absent(self):
        blob = self._all_text()
        self.assertNotIn('subject-secret-device-token', blob)
        # ...but the device itself is still disclosed.
        zf, _names, _fn = open_archive(self.user)
        self.assertEqual(len(read_json(zf, 'notifications.json')['registered_devices']), 1)

    def test_emergency_card_token_is_absent(self):
        token = str(self.user.patient_profile.emergency_token)
        self.assertNotIn(token, self._all_text())

    def test_api_keys_and_settings_are_absent(self):
        with self.settings(GROQ_API_KEY='groq-secret-key-xyz',
                           SECRET_KEY='django-secret-xyz'):
            blob = self._all_text()
        self.assertNotIn('groq-secret-key-xyz', blob)
        self.assertNotIn('django-secret-xyz', blob)

    def test_exclusions_are_documented_in_the_manifest(self):
        zf, _names, _fn = open_archive(self.user)
        items = ' '.join(e['item'] for e in read_json(zf, 'manifest.json')['exclusions'])
        self.assertIn('password', items)
        self.assertIn('token', items)


@NO_AUTOINDEX
class ExportIsolationTests(_FixtureMixin, TestCase):
    """No other user's data — records, files, or identity — may appear."""

    def setUp(self):
        cache.clear()
        self.subject, _ = self.build_populated_user('subject')
        self.other, self.other_record = self.build_populated_user('intruder')

    def _all_text(self):
        zf, names, _fn = open_archive(self.subject)
        return '\n'.join(
            zf.read(n).decode('utf-8', errors='replace')
            for n in names if n.endswith('.json')
        )

    def test_other_users_records_are_absent(self):
        blob = self._all_text()
        self.assertIn('subject Bloodwork', blob)
        self.assertNotIn('intruder Bloodwork', blob)
        self.assertNotIn('intruder question about creatinine', blob)
        self.assertNotIn('intruder high risk', blob)

    def test_other_users_files_are_absent(self):
        zf, names, _fn = open_archive(self.subject)
        blobs = b''.join(zf.read(n) for n in names if n.startswith('files/'))
        self.assertNotIn(b'intruder', blobs)
        manifest = read_json(zf, 'manifest.json')
        for entry in manifest['files']:
            self.assertNotIn('intruder', (entry['original_filename'] or ''))

    def test_counts_match_only_the_subjects_rows(self):
        zf, _names, _fn = open_archive(self.subject)
        self.assertEqual(len(read_json(zf, 'medical_records.json')), 1)
        self.assertEqual(len(read_json(zf, 'conversations.json')), 1)
        self.assertEqual(len(read_json(zf, 'predictions.json')), 1)

    def test_clinician_own_access_log_is_excluded_but_subject_side_included(self):
        """
        A doctor exporting their data must not receive their patients' records
        of access; a patient must receive the log of who read theirs.
        """
        doctor = User.objects.create_user(
            username='doc', email='doc@example.com', password='pw-doc-1', role='doctor',
        )
        PatientDoctorRelationship.objects.create(
            patient=self.subject, doctor=doctor, is_active=True,
        )
        DoctorAccessLog.objects.create(
            actor=doctor, patient=self.subject, resource='patient_records',
        )
        DoctorAccessLog.objects.create(
            actor=doctor, patient=self.other, resource='patient_records',
        )

        # Patient's export: sees the access to their own records.
        zf, _names, _fn = open_archive(self.subject)
        history = read_json(zf, 'access_history.json')
        self.assertEqual(len(history['clinician_access_to_my_records']), 1)
        self.assertEqual(len(history['linked_clinicians']), 1)

        # Doctor's export: no patient access entries at all.
        zf2, _names2, _fn2 = open_archive(doctor)
        doctor_history = read_json(zf2, 'access_history.json')
        self.assertEqual(doctor_history['clinician_access_to_my_records'], [])
        blob = zf2.read('access_history.json').decode('utf-8')
        self.assertNotIn('intruder', blob)
        self.assertNotIn('subject', blob)


@NO_AUTOINDEX
class ExportFileHandlingTests(_FixtureMixin, TestCase):

    def setUp(self):
        cache.clear()
        self.user, self.record = self.build_populated_user()

    def test_uploaded_files_are_included_and_referenced(self):
        zf, names, _fn = open_archive(self.user)
        file_entries = [n for n in names if n.startswith('files/')]
        self.assertTrue(file_entries)

        records = read_json(zf, 'medical_records.json')
        referenced = records[0]['attached_file']
        self.assertIn(referenced, names)
        self.assertEqual(zf.read(referenced), b'%PDF-1.4 fake')

    def test_manifest_lists_each_included_file(self):
        zf, _names, _fn = open_archive(self.user)
        files = read_json(zf, 'manifest.json')['files']
        self.assertTrue(files)
        for entry in files:
            if entry['included']:
                self.assertTrue(entry['path'].startswith('files/'))
                self.assertIsInstance(entry['size_bytes'], int)

    def test_archive_paths_cannot_escape_the_archive(self):
        """A hostile stored filename must not produce a traversing entry."""
        self.record.file.name = '../../../../etc/passwd'
        self.record.save(update_fields=['file'])

        zf, names, _fn = open_archive(self.user)
        for name in names:
            self.assertFalse(name.startswith('/'), name)
            self.assertFalse(name.startswith('..'), name)
            self.assertNotIn('..', name.split('/'), name)
            self.assertNotIn('\\', name, name)

    def test_missing_blob_does_not_break_the_export(self):
        """Storage can lose a file (ephemeral disk); metadata still belongs to the user."""
        self.record.file.name = 'medical_records/2026/01/does-not-exist.pdf'
        self.record.save(update_fields=['file'])

        zf, names, _fn = open_archive(self.user)
        self.assertIn('manifest.json', names)
        entries = read_json(zf, 'manifest.json')['files']
        missing = [e for e in entries if not e['included']]
        self.assertTrue(missing)
        self.assertIn('not available', missing[0]['reason'])
        # The record metadata is still exported.
        self.assertEqual(len(read_json(zf, 'medical_records.json')), 1)


@NO_AUTOINDEX
class ExportRobustnessTests(_FixtureMixin, TestCase):

    def setUp(self):
        cache.clear()

    def test_user_with_no_data_still_gets_a_valid_archive(self):
        empty = User.objects.create_user(
            username='empty', email='empty@example.com', password='pw-empty-1',
        )
        zf, names, _fn = open_archive(empty)
        self.assertIn('manifest.json', names)
        self.assertEqual(read_json(zf, 'medical_records.json'), [])
        self.assertEqual(read_json(zf, 'conversations.json'), [])
        self.assertEqual(read_json(zf, 'appointments.json'), [])
        # Consent still lists every purpose, all ungranted.
        consent = read_json(zf, 'consent.json')
        self.assertTrue(consent['current'])
        self.assertFalse(any(c['granted'] for c in consent['current']))
        self.assertEqual(read_json(zf, 'manifest.json')['files'], [])

    def test_user_without_patient_profile_is_handled(self):
        user = User.objects.create_user(
            username='noprof', email='noprof@example.com', password='pw-noprof-1',
        )
        zf, _names, _fn = open_archive(user)
        account = read_json(zf, 'user.json')
        self.assertNotIn('patient_profile', account)
        self.assertEqual(read_json(zf, 'access_history.json')['emergency_card_views'], [])

    # Note: the database rejects NaN/Infinity in a JSONField outright — SQLite
    # via `CHECK (JSON_VALID(...))`, Postgres via jsonb parsing — so such a row
    # cannot be written through the ORM. The serialiser is still hardened
    # against it (legacy rows, raw SQL, a future backend change), so the
    # defence is tested directly rather than through a row that cannot exist.

    def test_serialiser_survives_values_json_cannot_represent(self):
        from apps.accounts.export import _safe_json

        hostile = {
            'nan':   float('nan'),
            'inf':   float('inf'),
            'ninf':  float('-inf'),
            'bytes': b'\x00\x01binary',
            'set':   {'b', 'a'},
            'nested': {'deep': [float('nan'), {'more': float('-inf')}]},
        }
        parsed = json.loads(_safe_json(hostile))   # must be strict-parseable
        self.assertIsNone(parsed['nan'])
        self.assertIsNone(parsed['inf'])
        self.assertIsNone(parsed['ninf'])
        self.assertIn('bytes', parsed)
        self.assertIsNone(parsed['nested']['deep'][0])

    def test_serialiser_survives_pathological_nesting(self):
        from apps.accounts.export import _safe_json

        deep = current = {}
        for _ in range(200):
            current['next'] = {}
            current = current['next']
        json.loads(_safe_json({'deep': deep}))   # must not raise

    def test_awkward_but_storable_json_content_round_trips(self):
        """Unicode, nulls, nesting and long strings must survive the export."""
        user, record = self.build_populated_user('weird', with_files=False)
        record.parsed_data = {
            'unicode':   'Käypä hoito — kreatiniini 250 µmol/l 日本語',
            'null':      None,
            'nested':    [{'a': [1, 2, {'b': None}]}],
            'long_text': 'x' * 20000,
            'empty':     {},
        }
        record.save(update_fields=['parsed_data'])

        zf, names, _fn = open_archive(user)
        self.assertIn('medical_records.json', names)
        parsed = read_json(zf, 'medical_records.json')[0]['parsed_data']
        self.assertIn('Käypä hoito', parsed['unicode'])
        self.assertIsNone(parsed['null'])
        self.assertEqual(len(parsed['long_text']), 20000)

    def test_every_json_file_is_strictly_parseable(self):
        user, _ = self.build_populated_user('strict', with_files=False)
        zf, names, _fn = open_archive(user)
        for name in names:
            if name.endswith('.json'):
                with self.subTest(file=name):
                    json.loads(zf.read(name).decode('utf-8'))


@NO_AUTOINDEX
class ExportWebViewTests(_FixtureMixin, TestCase):

    def setUp(self):
        cache.clear()
        self.user, _ = self.build_populated_user()

    def tearDown(self):
        cache.clear()

    def test_page_requires_login(self):
        resp = self.client.get(reverse('accounts:data_export'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

    def test_download_requires_login(self):
        resp = self.client.post(reverse('accounts:data_export'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

    def test_post_returns_a_zip_the_user_can_open(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse('accounts:data_export'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/zip')
        self.assertIn('attachment', resp['Content-Disposition'])

        payload = b''.join(resp.streaming_content)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            self.assertIn('manifest.json', zf.namelist())
            self.assertEqual(
                json.loads(zf.read('manifest.json'))['subject']['username'], 'subject')

    def test_response_is_not_cacheable(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse('accounts:data_export'))
        self.assertIn('no-store', resp['Cache-Control'])


@NO_AUTOINDEX
class ExportAPITests(_FixtureMixin, APITestCase):

    def setUp(self):
        cache.clear()
        self.user, _ = self.build_populated_user()
        self.other, _ = self.build_populated_user('intruder')

    def tearDown(self):
        cache.clear()

    def test_status_and_download_require_authentication(self):
        for url in ('/api/v1/export/', '/api/v1/export/download/'):
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (401, 403))

    def test_status_reports_ready_and_the_schema_version(self):
        self.client.force_authenticate(user=self.user)
        body = self.client.get('/api/v1/export/').json()
        self.assertEqual(body['state'], 'ready')
        self.assertEqual(body['export_version'], EXPORT_VERSION)
        self.assertIn('medical_records', body['data_categories'])
        self.assertTrue(body['exclusions'])

    def test_download_returns_only_the_callers_data(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get('/api/v1/export/download/')
        self.assertEqual(resp.status_code, 200)

        payload = b''.join(resp.streaming_content)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            blob = '\n'.join(
                zf.read(n).decode('utf-8', errors='replace')
                for n in zf.namelist() if n.endswith('.json')
            )
        self.assertIn('subject Bloodwork', blob)
        self.assertNotIn('intruder Bloodwork', blob)

    def test_no_user_parameter_can_redirect_the_subject(self):
        """A supplied identifier must be ignored, not honoured."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            f'/api/v1/export/download/?user={self.other.pk}&user_id={self.other.pk}'
            f'&username=intruder&email=intruder@example.com'
        )
        self.assertEqual(resp.status_code, 200)
        payload = b''.join(resp.streaming_content)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            manifest = json.loads(zf.read('manifest.json'))
        self.assertEqual(manifest['subject']['username'], 'subject')

    def test_failure_returns_a_clear_error_state(self):
        from unittest.mock import patch

        self.client.force_authenticate(user=self.user)
        with patch('apps.api.views.export.build_export', side_effect=RuntimeError('boom')):
            resp = self.client.get('/api/v1/export/download/')
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()['state'], 'failed')
        self.assertIn('error', resp.json())

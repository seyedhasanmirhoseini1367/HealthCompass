"""Prompt 6 of the production-readiness audit: queued ingestion (ARCH-02),
scheduled jobs and heartbeats (ARCH-03), pgvector retrieval (ARCH-05), media
through storage (ARCH-06) and Sentry scrubbing (ARCH-09)."""
from datetime import timedelta
from io import StringIO
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.medical_records.models import MedicalRecord
from apps.medical_records.services import process_record, queue_text
from apps.ops.heartbeat import heartbeat
from apps.ops.models import JobHeartbeat
from apps.ops.schedule import SCHEDULE, due_jobs, stale_jobs

User = get_user_model()
S = MedicalRecord.ProcessingStatus


class QueuedIngestionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('q_user', email='q@test.invalid', password='pw')

    @override_settings(RAG_AUTO_INDEX_SYNC=False, RQ_ASYNC=True)
    def test_upload_returns_before_parsing(self):
        with patch('apps.medical_records.services._parse_text') as parse, \
                patch('django_rq.get_queue') as get_queue:
            with self.captureOnCommitCallbacks(execute=True):
                result = queue_text(self.user, 'Hb 140 g/L')
        parse.assert_not_called()
        self.assertEqual(result['record'].processing_status, S.QUEUED)
        get_queue.assert_called_with('ingest')

    def test_status_moves_from_queued_to_ready(self):
        with override_settings(RAG_AUTO_INDEX_SYNC=False, RQ_ASYNC=False):
            record = queue_text(self.user, 'Hb 140 g/L')['record']   # on_commit never fires here
        self.assertEqual(record.processing_status, S.QUEUED)
        process_record(str(record.pk))
        record.refresh_from_db()
        self.assertEqual(record.processing_status, S.READY)
        process_record(str(record.pk))  # a second run is a no-op, not a re-parse

    def test_a_parser_exception_gives_failed_with_a_generic_message(self):
        with override_settings(RAG_AUTO_INDEX_SYNC=False, RQ_ASYNC=False):
            record = queue_text(self.user, 'Hb 140 g/L')['record']
        with patch('apps.medical_records.services._parse_text',
                   side_effect=RuntimeError('quoted patient text 140 g/L')), \
                patch('healthcompass.observability.emit') as ops:
            process_record(str(record.pk))
        record.refresh_from_db()
        self.assertEqual(record.processing_status, S.FAILED)
        self.assertNotIn('140', record.processing_error)
        self.assertTrue(ops.called)

    def test_status_endpoints_and_retry(self):
        with override_settings(RAG_AUTO_INDEX_SYNC=False, RQ_ASYNC=False):
            record = queue_text(self.user, 'Hb 140 g/L')['record']
        MedicalRecord.objects.filter(pk=record.pk).update(processing_status=S.FAILED)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(f'/records/{record.pk}/status/').json()['status'], 'failed')
        self.client.post(f'/records/{record.pk}/retry/')   # runs inline under the test runner
        record.refresh_from_db()
        self.assertEqual(record.processing_status, S.READY)

    def test_another_patients_status_is_404(self):
        other = User.objects.create_user('q_other', email='qo@test.invalid', password='pw')
        record = MedicalRecord.objects.create(patient=other, title='x')
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(f'/records/{record.pk}/status/').status_code, 404)


class HeartbeatTests(TestCase):
    def test_success_is_recorded(self):
        @heartbeat('unit_ok')
        def job():
            return 1
        job()
        beat = JobHeartbeat.objects.get(name='unit_ok')
        self.assertIsNotNone(beat.last_succeeded)
        self.assertEqual(beat.last_error, '')

    def test_failure_is_recorded_and_raised(self):
        @heartbeat('unit_fail')
        def job():
            raise ValueError('boom')
        with self.assertRaises(ValueError):
            job()
        beat = JobHeartbeat.objects.get(name='unit_fail')
        self.assertIsNone(beat.last_succeeded)
        self.assertEqual(beat.last_error, 'ValueError')

    def test_due_and_stale(self):
        now = timezone.now()
        names = [e[0] for e in SCHEDULE if e[3] is None]
        self.assertTrue(set(names) <= set(due_jobs(now)))
        for name in names:
            JobHeartbeat.objects.create(name=name, last_started=now, last_succeeded=now)
        self.assertEqual([n for n in due_jobs(now) if n in names], [])
        self.assertEqual([n for n in stale_jobs(now) if n in names], [])
        later = now + timedelta(days=10)
        self.assertIn('purge_old_query_logs', stale_jobs(later))

    def test_readiness_reports_a_stale_job_as_degraded(self):
        response = self.client.get('/health/ready/')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['status'], 'degraded')
        self.assertIn('run_care_cycle', body['jobs']['stale'])

    def test_scheduler_once_runs_due_jobs(self):
        from django.core.management import call_command
        with patch('apps.ops.schedule.run_job') as run, \
                patch('apps.ops.management.commands.run_scheduler.run_job', run):
            call_command('run_scheduler', '--once', stdout=StringIO())
        self.assertTrue(run.called)


class MediaThroughStorageTests(TestCase):
    def test_files_are_read_through_default_storage(self):
        storage = InMemoryStorage()
        user = User.objects.create_user('m_user', email='m@test.invalid', password='pw')
        name = storage.save('records/report.pdf', ContentFile(b'%PDF-1.4 body'))
        MedicalRecord.objects.create(patient=user, title='r', file=name)
        self.client.force_login(user)
        with patch('django.core.files.storage.default_storage', storage):
            response = self.client.get(f'/media/{name}')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'%PDF-1.4 body')
            self.assertEqual(response['Cache-Control'], 'no-store')
            self.assertEqual(self.client.get('/media/records/../secrets.pdf').status_code, 404)

    def test_profile_pictures_are_privately_cacheable(self):
        storage = InMemoryStorage()
        user = User.objects.create_user('m_pic', email='mp@test.invalid', password='pw')
        name = storage.save('profile_pics/me.png', ContentFile(b'\x89PNG\r\n\x1a\n'))
        user.profile_picture = name
        user.save()
        self.client.force_login(user)
        with patch('django.core.files.storage.default_storage', storage):
            response = self.client.get(f'/media/{name}')
        self.assertEqual(response['Cache-Control'], 'private, max-age=3600')
        self.assertTrue(response['ETag'])


class SentryScrubbingTests(TestCase):
    def test_bodies_queries_and_secrets_are_removed(self):
        from healthcompass.sentry import before_send
        event = {'request': {'url': 'https://x/assistant/?q=my+creatinine', 'query_string': 'q=1',
                             'data': {'message': 'my creatinine'}, 'headers': {'Authorization': 'Bearer t'}},
                 'extra': {'query': 'secret question', 'record_id': 'abc'}}
        out = before_send(event, {})
        self.assertNotIn('data', out['request'])
        self.assertNotIn('query_string', out['request'])
        self.assertEqual(out['request']['url'], 'https://x/assistant/')
        self.assertEqual(out['request']['headers']['Authorization'], '[removed]')
        self.assertEqual(out['extra']['query'], '[removed]')
        self.assertEqual(out['extra']['record_id'], 'abc')


@skipUnless(connection.vendor == 'postgresql', 'pgvector runs on PostgreSQL only')
class PgvectorIsolationTests(TestCase):
    def test_another_patients_chunks_are_never_candidates(self):
        import numpy as np

        from apps.rag_assistant.models import MedicalChunk, MedicalDocument
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        alice = User.objects.create_user('pv_a', email='pva@test.invalid', password='pw')
        bob = User.objects.create_user('pv_b', email='pvb@test.invalid', password='pw')
        vec = np.ones(3072, dtype=np.float32)
        for owner in (alice, bob):
            doc = MedicalDocument.objects.create(patient=owner, title='d', document_type='raw_text',
                                                 content='c')
            for i in range(3):
                MedicalChunk.objects.create(document=doc, patient=owner, content=f'{owner.pk}-{i}',
                                            chunk_index=i, embedding=vec.tobytes(), embedding_vec=vec,
                                            embedding_model='models/gemini-embedding-001',
                                            embedding_dimensions=3072)
        texts, _, _ = EmbeddingService().load_patient_embeddings(alice, query_vector=vec, candidates=10)
        self.assertTrue(texts)
        self.assertTrue(all(t.startswith(f'{alice.pk}-') for t in texts))

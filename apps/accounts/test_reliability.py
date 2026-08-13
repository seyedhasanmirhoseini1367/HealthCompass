"""
Production reliability regression tests.

These pin operational properties that are invisible until they fail in
production: that a hung provider cannot pin a worker, that concurrent writes do
not lose updates, that a bulk import cannot spawn unbounded threads, and that
clinical text never reaches the application log.
"""
import logging
import threading
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APITestCase

from apps.ai_insights.models import AIModel

User = get_user_model()
NO_AUTOINDEX = override_settings(RAG_AUTO_INDEX_SYNC=False)


class HealthAndReadinessTests(TestCase):
    """Liveness and readiness must answer different questions."""

    def test_liveness_does_not_depend_on_the_database(self):
        """A restart cannot fix a down database, so liveness must not check it."""
        with patch('django.db.connection.cursor', side_effect=RuntimeError('db down')):
            resp = self.client.get('/health/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'ok')

    def test_readiness_reports_ok_when_dependencies_are_up(self):
        resp = self.client.get('/health/ready/')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['status'], 'ready')
        self.assertEqual(body['checks']['database'], 'ok')
        self.assertEqual(body['checks']['cache'], 'ok')

    def test_readiness_returns_503_when_the_database_is_unreachable(self):
        with patch('django.db.connection.cursor', side_effect=RuntimeError('db down')):
            resp = self.client.get('/health/ready/')
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body['status'], 'not_ready')
        self.assertEqual(body['checks']['database'], 'error')

    def test_readiness_does_not_leak_failure_detail(self):
        """Connection errors can carry credentials; the body must stay opaque."""
        with patch('django.db.connection.cursor',
                   side_effect=RuntimeError('could not connect: password=hunter2')):
            resp = self.client.get('/health/ready/')
        self.assertNotIn('hunter2', resp.content.decode())
        self.assertNotIn('password', resp.content.decode())

    def test_readiness_reports_cache_failure_separately(self):
        with patch('django.core.cache.cache.set', side_effect=RuntimeError('redis down')):
            resp = self.client.get('/health/ready/')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()['checks']['cache'], 'error')
        self.assertEqual(resp.json()['checks']['database'], 'ok')


class ProviderTimeoutTests(TestCase):
    """
    Every external model call must carry an explicit deadline.

    gunicorn kills a worker at 120s; the OpenAI and Anthropic SDKs default to
    600s. Without an explicit timeout a handful of hung upstream requests can
    occupy every thread in the pool until the worker is killed mid-request.
    """

    def _capture_client_kwargs(self, module_name, attr, call):
        captured = {}

        def _factory(*args, **kwargs):
            captured.update(kwargs)
            client = MagicMock()
            client.chat.completions.create.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(content='ok'))])
            return client

        module = MagicMock()
        setattr(module, attr, _factory)
        with patch.dict('sys.modules', {module_name: module}):
            call()
        return captured

    def test_groq_generation_sets_a_timeout(self):
        from apps.rag_assistant.services.generation_service import _call_groq

        with self.settings(GROQ_API_KEY='k'):
            kwargs = self._capture_client_kwargs(
                'groq', 'Groq', lambda: _call_groq('ctx', 'q', []))
        self.assertIn('timeout', kwargs)
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLess(kwargs['timeout'], 120,
                        'provider timeout must stay under the gunicorn worker timeout')

    def test_openai_generation_sets_a_timeout(self):
        from apps.rag_assistant.services.generation_service import _call_openai

        with self.settings(OPENAI_API_KEY='k'):
            kwargs = self._capture_client_kwargs(
                'openai', 'OpenAI', lambda: _call_openai('ctx', 'q', []))
        self.assertIn('timeout', kwargs)

    def test_anthropic_generation_sets_a_timeout(self):
        from apps.rag_assistant.services.generation_service import _call_anthropic

        captured = {}

        def _factory(*args, **kwargs):
            captured.update(kwargs)
            client = MagicMock()
            client.messages.create.return_value = MagicMock(
                content=[MagicMock(text='ok')])
            return client

        module = MagicMock()
        module.Anthropic = _factory
        with patch.dict('sys.modules', {'anthropic': module}), \
             self.settings(ANTHROPIC_API_KEY='k'):
            _call_anthropic('ctx', 'q', [])
        self.assertIn('timeout', captured)

    def test_gemini_client_sets_a_timeout(self):
        from apps.rag_assistant.services.generation_service import _gemini_client

        genai = MagicMock()
        _gemini_client(genai, 'k')
        kwargs = genai.Client.call_args.kwargs
        self.assertIn('http_options', kwargs)
        # google-genai expects milliseconds.
        self.assertGreater(kwargs['http_options']['timeout'], 1000)

    def test_gemini_client_falls_back_when_the_sdk_rejects_the_option(self):
        """A version difference must not break generation entirely."""
        from apps.rag_assistant.services.generation_service import _gemini_client

        genai = MagicMock()
        genai.Client.side_effect = [TypeError('unexpected kwarg'), MagicMock()]
        client = _gemini_client(genai, 'k')
        self.assertIsNotNone(client)
        self.assertEqual(genai.Client.call_count, 2)

    def test_a_raising_provider_falls_through_to_the_next(self):
        """One provider failing must never end the request."""
        from apps.rag_assistant.services import generation_service as gs

        with patch.object(gs, '_call_groq', side_effect=TimeoutError('hung')), \
             patch.object(gs, '_call_gemini', side_effect=RuntimeError('sdk bug')), \
             patch.object(gs, '_call_anthropic', return_value=None), \
             patch.object(gs, '_call_openai', return_value='answer from the 4th provider'):
            answer, _sources, provider = gs.generate([], 'q', [])

        self.assertEqual(provider, 'openai')
        self.assertEqual(answer, 'answer from the 4th provider')

    def test_every_provider_raising_still_returns_the_static_fallback(self):
        from apps.rag_assistant.services import generation_service as gs

        with patch.object(gs, '_call_groq', side_effect=TimeoutError('hung')), \
             patch.object(gs, '_call_gemini', side_effect=RuntimeError('boom')), \
             patch.object(gs, '_call_anthropic', side_effect=ValueError('boom')), \
             patch.object(gs, '_call_openai', side_effect=OSError('boom')):
            answer, _sources, provider = gs.generate([], 'q', [])

        self.assertEqual(provider, 'fallback')
        self.assertTrue(answer)

    def test_generation_returns_a_fallback_when_every_provider_is_down(self):
        from apps.rag_assistant.services import generation_service as gs

        with patch.object(gs, '_call_groq', return_value=None), \
             patch.object(gs, '_call_gemini', return_value=None), \
             patch.object(gs, '_call_anthropic', return_value=None), \
             patch.object(gs, '_call_openai', return_value=None):
            answer, sources, provider = gs.generate([], 'q', [])
        self.assertEqual(provider, 'fallback')
        self.assertTrue(answer)


@NO_AUTOINDEX
class ConcurrentCounterTests(TransactionTestCase):
    """
    run_count was a read-modify-write: read into Python, add one, write back.
    Two concurrent predictions read the same value and one increment is lost.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='cnt', email='cnt@example.com', password='pw-count-1',
        )
        self.model = AIModel.objects.create(
            data_scientist=self.user, name='Counter', description='d', status='active',
        )

    def test_concurrent_increments_do_not_lose_updates(self):
        from django.db.models import F

        threads, iterations = 4, 10

        def bump():
            try:
                for _ in range(iterations):
                    AIModel.objects.filter(pk=self.model.pk).update(
                        run_count=F('run_count') + 1)
            finally:
                connection.close()

        workers = [threading.Thread(target=bump) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        self.model.refresh_from_db()
        self.assertEqual(self.model.run_count, threads * iterations)

    def test_views_use_an_atomic_increment(self):
        """Pins the pattern: a stale in-memory value must never be written back."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob('*.py'):
            rel = path.relative_to(root).as_posix()
            if 'test' in rel or 'migrations/' in rel:
                continue
            source = path.read_text(encoding='utf-8-sig', errors='strict')
            if '.run_count + 1' in source:
                offenders.append(rel)
        self.assertEqual(offenders, [],
                         f'read-modify-write on run_count in: {offenders}')


@NO_AUTOINDEX
class BackgroundIndexingTests(TransactionTestCase):
    """
    Indexing used to spawn one thread per saved record. A Kanta import creates
    one record per document in a loop, so a large import meant hundreds of
    threads, each with its own database connection and embedding API call.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='idx', email='idx@example.com', password='pw-idx-1',
        )

    def test_bulk_creates_do_not_spawn_a_thread_per_record(self):
        from apps.medical_records.models import MedicalRecord

        before = threading.active_count()
        with patch('apps.rag_assistant.signals._index_in_background'):
            for i in range(25):
                MedicalRecord.objects.create(
                    patient=self.user, title=f'Record {i}', record_type='other',
                )
        after = threading.active_count()
        # The pool is capped; 25 records must not add 25 threads.
        self.assertLess(after - before, 10,
                        f'thread count grew by {after - before} for 25 records')

    def test_indexing_pool_is_bounded(self):
        from apps.rag_assistant.signals import _INDEX_WORKERS, _get_executor

        executor = _get_executor()
        self.assertLessEqual(executor._max_workers, _INDEX_WORKERS)

    def test_indexing_failure_does_not_propagate_to_the_caller(self):
        """A failed index must not fail the upload that triggered it."""
        from apps.medical_records.models import MedicalRecord
        from apps.rag_assistant.signals import _index_in_background

        with patch('apps.rag_assistant.services.rag_service.RAGService.index_record',
                   side_effect=RuntimeError('embedding provider down')):
            record = MedicalRecord.objects.create(
                patient=self.user, title='Still saved', record_type='other',
            )
            _index_in_background(str(record.pk))   # must not raise

        self.assertTrue(MedicalRecord.objects.filter(pk=record.pk).exists())


@NO_AUTOINDEX
class PhiLoggingTests(TestCase):
    """Clinical content must not reach ordinary application logs."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='log', email='log@example.com', password='pw-log-1',
        )

    def test_router_does_not_log_the_question_text(self):
        from apps.rag_assistant.graph import nodes

        secret = 'do I have pancreatic cancer'
        with self.assertLogs('apps.rag_assistant.graph.nodes', level='DEBUG') as captured:
            nodes.logger.debug('router_node: route=%s q_len=%d', 'records', len(secret))
        self.assertNotIn(secret, '\n'.join(captured.output))

    def test_no_source_line_logs_raw_question_text(self):
        """Pins the fix: no logging call may interpolate the question."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob('*.py'):
            rel = path.relative_to(root).as_posix()
            if 'test' in rel or 'migrations/' in rel:
                continue
            for i, line in enumerate(path.read_text(encoding='utf-8-sig',
                                                    errors='strict').splitlines(), 1):
                stripped = line.strip()
                if not stripped.startswith(('logger.', 'logging.')):
                    continue
                if 'question[' in stripped or 'q=%r' in stripped or 'query[:' in stripped:
                    offenders.append(f'{rel}:{i}')
        self.assertEqual(offenders, [],
                         f'clinical text interpolated into a log call: {offenders}')


class DemoEndpointAbuseTests(TestCase):
    """The unauthenticated demo endpoints are CPU-bound and must be bounded."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_realtime_load_rejects_an_oversized_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with override_settings(MAX_UPLOAD_BYTES=1024):
            resp = self.client.post(
                '/insights/seizure-realtime/load/',
                {'files': SimpleUploadedFile('big.csv', b'a,b\n' + b'1,2\n' * 5000)},
            )
        self.assertEqual(resp.status_code, 413)
        self.assertIn('too large', resp.json()['error'].lower())

    def test_realtime_load_is_rate_limited(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        statuses = []
        for _ in range(45):
            statuses.append(self.client.post(
                '/insights/seizure-realtime/load/',
                {'files': SimpleUploadedFile('x.csv', b'a,b\n1,2\n')},
            ).status_code)
        self.assertIn(429, statuses, f'demo endpoint was never rate limited: {statuses}')

    def test_predict_chunk_is_rate_limited(self):
        import json as _json

        statuses = []
        for _ in range(250):
            statuses.append(self.client.post(
                '/insights/seizure-realtime/predict-chunk/',
                data=_json.dumps({'window': []}), content_type='application/json',
            ).status_code)
            if statuses[-1] == 429:
                break
        self.assertIn(429, statuses)


@NO_AUTOINDEX
class TabularImportLimitTests(APITestCase):
    """Compressed tabular formats must not expand without bound."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='tab', email='tab@example.com', password='pw-tab-1',
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        cache.clear()

    @override_settings(MAX_PARSED_ROWS=10)
    def test_parquet_row_explosion_is_rejected_before_materialising(self):
        import io as _io

        import pandas as pd
        from apps.medical_records.parsers import WearableCSVParser

        buffer = _io.BytesIO()
        pd.DataFrame({'datetime': ['2026-01-01'] * 500,
                      'heart_rate': list(range(500))}).to_parquet(buffer)

        result = WearableCSVParser().parse(buffer.getvalue(), 'bomb.parquet')
        self.assertEqual(result['count'], 0)
        self.assertTrue(result['errors'])
        self.assertIn('exceeds', result['errors'][0])

    @override_settings(MAX_PARSED_ROWS=10_000)
    def test_normal_wearable_parquet_still_imports(self):
        import io as _io

        import pandas as pd
        from apps.medical_records.parsers import WearableCSVParser

        buffer = _io.BytesIO()
        pd.DataFrame({'datetime': ['2026-01-01T10:00:00'] * 5,
                      'heart_rate': [60, 61, 62, 63, 64]}).to_parquet(buffer)

        result = WearableCSVParser().parse(buffer.getvalue(), 'ok.parquet')
        self.assertEqual(result['errors'][:1] or [''], [''])
        self.assertGreater(result['count'], 0)

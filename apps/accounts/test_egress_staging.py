"""
Pre-flight tests for switching CONSENT_ENFORCED_EGRESS from 'rag' to 'all'.

test_egress.py proves each guard works in isolation. This module proves the
properties that make the production flip safe:

  * every PHI-bearing point is fail-closed under 'all', and the non-PHI point
    is not;
  * RAGService really is the only way into generation, which is what lets the
    consent gate live there instead of inside generation_service;
  * a real upload — record save → post_save signal → indexing → embedding —
    never reaches the provider without consent;
  * refusal responses stay readable by clients written before consent existed.
"""
import pathlib
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.accounts.consent import grant_consent, revoke_consent
from apps.accounts.egress import EGRESS_POINTS, ExternalProcessingGuard
from apps.accounts.models import ConsentPurpose

User = get_user_model()
EXTERNAL = ConsentPurpose.EXTERNAL_LLM

ENFORCE_ALL = override_settings(CONSENT_ENFORCED_EGRESS=['all'])
STALE_VERSION = {**{k: 'v1' for k in ConsentPurpose.values}, EXTERNAL: 'v2'}


class FailClosedReadinessTests(TestCase):
    """Under 'all', every PHI point must deny; the non-PHI point must not."""

    #: The points that must be protected before 'all' is enabled in production.
    EXPECTED_PROTECTED = [
        'rag.generation', 'rag.embed_query', 'rag.rerank', 'rag.classify',
        'rag.embed_documents', 'records.parse', 'records.ocr',
        'insights.interpretation', 'insights.seizure_proxy',
        'insights.seizure_interpretation',
    ]

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='fc', email='fc@example.com', password='pw-failclosed-1',
        )

    def test_expected_set_matches_the_registry(self):
        """No PHI point may exist outside the reviewed list, in either direction."""
        registered_phi = {p.id for p in EGRESS_POINTS.values() if p.phi}
        self.assertEqual(registered_phi, set(self.EXPECTED_PROTECTED))

    @ENFORCE_ALL
    def test_every_phi_point_denies_without_consent(self):
        for point_id in self.EXPECTED_PROTECTED:
            with self.subTest(point=point_id):
                self.assertTrue(ExternalProcessingGuard.is_enforced(point_id))
                self.assertFalse(ExternalProcessingGuard.allows(self.user, point_id))

    @ENFORCE_ALL
    def test_every_phi_point_denies_anonymous_and_missing_user(self):
        from django.contrib.auth.models import AnonymousUser
        for point_id in self.EXPECTED_PROTECTED:
            with self.subTest(point=point_id):
                self.assertFalse(ExternalProcessingGuard.allows(AnonymousUser(), point_id))
                self.assertFalse(ExternalProcessingGuard.allows(None, point_id))

    @ENFORCE_ALL
    def test_every_phi_point_allows_with_valid_consent(self):
        grant_consent(self.user, EXTERNAL)
        for point_id in self.EXPECTED_PROTECTED:
            with self.subTest(point=point_id):
                self.assertTrue(ExternalProcessingGuard.allows(self.user, point_id))

    @ENFORCE_ALL
    def test_every_phi_point_denies_after_revocation(self):
        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        for point_id in self.EXPECTED_PROTECTED:
            with self.subTest(point=point_id):
                self.assertFalse(ExternalProcessingGuard.allows(self.user, point_id))

    @ENFORCE_ALL
    def test_every_phi_point_denies_on_stale_consent_version(self):
        grant_consent(self.user, EXTERNAL)
        with override_settings(CONSENT_VERSIONS=STALE_VERSION):
            for point_id in self.EXPECTED_PROTECTED:
                with self.subTest(point=point_id):
                    self.assertFalse(ExternalProcessingGuard.allows(self.user, point_id))

    @ENFORCE_ALL
    def test_public_knowledge_embedding_stays_unblocked_under_all(self):
        self.assertFalse(ExternalProcessingGuard.is_enforced('knowledge.embed'))
        self.assertTrue(ExternalProcessingGuard.allows(self.user, 'knowledge.embed'))
        self.assertTrue(ExternalProcessingGuard.allows(None, 'knowledge.embed'))


class GenerationEntryBoundaryTests(TestCase):
    """
    RAGService is the authoritative entry point to LLM generation.

    The consent gate lives in RAGService.ask()/stream_ask() rather than inside
    generation_service. That is only safe while nothing reaches generation
    another way, so this reads the source tree and fails if a new caller appears
    outside the sanctioned chain:

        RAGService.ask/stream_ask -> stream_graph -> graph nodes -> generate*
    """

    SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent

    ALLOWED_GRAPH_CALLERS = {'rag_assistant/services/rag_service.py'}
    ALLOWED_GENERATION_CALLERS = {
        'rag_assistant/graph/graph.py',
        'rag_assistant/graph/nodes.py',
    }

    def _callers(self, *function_names):
        """
        Modules containing a real call to any of *function_names*.

        Parsed with ast rather than grepped: a text search also matches the
        function's own `def` line and prose in docstrings, which produces false
        alarms and would train people to ignore this test.
        """
        import ast

        targets = set(function_names)
        hits = set()
        for path in self.SOURCE_ROOT.rglob('*.py'):
            rel = path.relative_to(self.SOURCE_ROOT).as_posix()
            if 'test' in rel or 'migrations/' in rel:
                continue
            if rel.endswith('services/generation_service.py'):
                continue   # the definitions themselves
            # utf-8-sig, not utf-8: several files in this repo carry a BOM, and
            # ast.parse rejects it as a non-printable character. Skipping
            # unparseable files would make this test silently blind to exactly
            # the bypass it exists to catch, so a parse failure is an error.
            source = path.read_text(encoding='utf-8-sig', errors='strict')
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                self.fail(f'could not parse {rel}, so it was never scanned: {exc}')
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute)
                        else None)
                if name in targets:
                    hits.add(rel)
        return hits

    def test_only_rag_service_enters_the_graph(self):
        callers = self._callers('stream_graph') - {'rag_assistant/graph/graph.py'}
        unexpected = callers - self.ALLOWED_GRAPH_CALLERS
        self.assertEqual(
            unexpected, set(),
            f'new graph entry point bypasses the RAGService consent gate: {unexpected}',
        )

    def test_only_graph_modules_call_generation(self):
        callers = self._callers('generate_streaming', 'generate')
        unexpected = callers - self.ALLOWED_GENERATION_CALLERS
        self.assertEqual(
            unexpected, set(),
            f'generation reached outside the RAGService chain: {unexpected}',
        )

    def test_both_rag_service_entry_points_call_the_gate(self):
        source = (self.SOURCE_ROOT / 'rag_assistant/services/rag_service.py').read_text(
            encoding='utf-8', errors='replace')
        self.assertEqual(
            source.count('enforce_for_ai('), 2,
            'ask() and stream_ask() must each call the consent gate',
        )


@ENFORCE_ALL
class UploadPipelineIntegrationTests(TestCase):
    """
    End-to-end upload, watching the embedding provider.

    Exercises the real chain (record save -> post_save signal ->
    RAGService.index_record -> chunking -> embed_chunks) rather than calling the
    guard directly, because that chain is the one the RAG gate never covered.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='up', email='up@example.com', password='pw-upload-1',
        )

    def _upload(self):
        from apps.medical_records.services import MedicalRecordService
        return MedicalRecordService.create_from_text(
            self.user, 'CREATININE 250 umol/L\nGLUCOSE 5.4 mmol/L',
        )

    @override_settings(RAG_AUTO_INDEX_SYNC=True)
    def test_upload_without_consent_never_reaches_the_provider(self):
        from apps.rag_assistant.models import MedicalChunk
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        with patch.object(EmbeddingService, '_call_api') as spy:
            result = self._upload()

        spy.assert_not_called()
        # The record is still created — refusing external processing must not
        # cost the patient their data.
        self.assertIsNotNone(result['record'].pk)
        chunks = MedicalChunk.objects.filter(patient=self.user)
        self.assertTrue(chunks.exists(), 'chunking is local and should still run')
        self.assertFalse(
            chunks.exclude(embedding=None).exists(),
            'no chunk may hold an embedding when the provider was never called',
        )

    @override_settings(RAG_AUTO_INDEX_SYNC=True)
    def test_upload_with_consent_reaches_the_provider(self):
        import numpy as np
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        grant_consent(self.user, EXTERNAL)
        with patch.object(
            EmbeddingService, '_call_api',
            side_effect=lambda texts, **kw: np.ones((len(texts), 3072), dtype=np.float32),
        ) as spy:
            self._upload()

        spy.assert_called()

    @override_settings(RAG_AUTO_INDEX_SYNC=True)
    def test_upload_after_revocation_never_reaches_the_provider(self):
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        with patch.object(EmbeddingService, '_call_api') as spy:
            self._upload()
        spy.assert_not_called()


@ENFORCE_ALL
class OcrFailClosedTests(TestCase):
    """ocr_image must be impossible to call without deciding whose image it is."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='oc', email='oc@example.com', password='pw-ocr-1',
        )

    def test_omitting_user_raises_rather_than_defaulting_to_permissive(self):
        from apps.medical_records.services import MedicalRecordService

        genai = MagicMock()
        with patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            with self.assertRaises(TypeError):
                MedicalRecordService.ocr_image(b'fake-image')
        genai.Client.assert_not_called()

    def test_anonymous_user_refused_regardless_of_enforcement_setting(self):
        from django.contrib.auth.models import AnonymousUser
        from apps.medical_records.services import MedicalRecordService

        genai = MagicMock()
        # Even with enforcement narrowed to 'rag', anonymous OCR is refused.
        with override_settings(CONSENT_ENFORCED_EGRESS=['rag']), \
             patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            result = MedicalRecordService.ocr_image(b'fake-image', user=AnonymousUser())
        genai.Client.assert_not_called()
        self.assertEqual(result['consent_required'], EXTERNAL)

    def test_none_user_refused(self):
        from apps.medical_records.services import MedicalRecordService

        genai = MagicMock()
        with patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            result = MedicalRecordService.ocr_image(b'fake-image', user=None)
        genai.Client.assert_not_called()
        self.assertIn('consent_required', result)

    def test_valid_user_without_consent_is_blocked(self):
        from apps.medical_records.services import MedicalRecordService

        genai = MagicMock()
        with patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            result = MedicalRecordService.ocr_image(b'fake-image', user=self.user)
        genai.Client.assert_not_called()
        self.assertEqual(result['consent_required'], EXTERNAL)

    def test_valid_user_with_consent_is_allowed(self):
        from apps.medical_records.services import MedicalRecordService

        grant_consent(self.user, EXTERNAL)
        genai = MagicMock()
        with patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            result = MedicalRecordService.ocr_image(b'fake-image', user=self.user)
        self.assertNotIn('consent_required', result)


@ENFORCE_ALL
class ConsentResponseContractTests(TestCase):
    """
    Refusals must stay readable by clients written before consent existed:
    a human-readable `error` plus a machine-readable `consent_required`.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='rc', email='rc@example.com', password='pw-contract-1',
        )

    def _api(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client

    def test_ocr_endpoint_contract(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        img = SimpleUploadedFile(
            'scan.png', b'\x89PNG\r\n\x1a\n' + b'0' * 64, content_type='image/png',
        )
        resp = self._api().post('/api/v1/records/upload/scan/',
                                {'image': img}, format='multipart')
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertIsInstance(body.get('error'), str)
        self.assertEqual(body['consent_required'], EXTERNAL)

    def test_seizure_endpoint_contract(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        eeg = SimpleUploadedFile('eeg.parquet', b'PAR1',
                                 content_type='application/octet-stream')
        resp = self._api().post('/api/v1/seizure-analysis/',
                                {'signal_file': eeg}, format='multipart')
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertIsInstance(body.get('error'), str)
        self.assertEqual(body['consent_required'], EXTERNAL)

    def test_assistant_stays_200_and_adds_the_field(self):
        """
        The chat endpoint keeps its 200 contract so existing clients render the
        refusal in the transcript; the new field lets newer clients do better.
        """
        resp = self._api().post('/api/v1/assistant/ask/', {'query': 'hi'}, format='json')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn('answer', body)
        self.assertEqual(body['consent_required'], EXTERNAL)
        self.assertIn('consent', body['answer'].lower())

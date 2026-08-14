"""
External egress consent enforcement tests.

Every test here asserts on the *provider call*, not on the HTTP response. A view
that returns 403 while still having shipped the bytes is exactly the failure this
phase exists to prevent, so each case patches the outbound client and asserts it
was never constructed or never called.

Enforcement is exercised with CONSENT_ENFORCED_EGRESS='all'. The production
default is narrower on purpose (see settings) — these tests prove the mechanism,
independent of which points a given deployment has switched on.
"""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.accounts.consent import (ConsentRequired, grant_consent, revoke_consent)
from apps.accounts.egress import (EGRESS_POINTS, ExternalProcessingGuard,
                                  egress_matrix)
from apps.accounts.models import ConsentPurpose

User = get_user_model()
EXTERNAL = ConsentPurpose.EXTERNAL_LLM

ENFORCE_ALL = override_settings(CONSENT_ENFORCED_EGRESS=['all'])
STALE_VERSION = {**{k: 'v1' for k in ConsentPurpose.values}, EXTERNAL: 'v2'}


class EgressMatrixTests(TestCase):
    """The registry itself is part of the privacy contract."""

    def test_every_point_declares_provider_and_data_category(self):
        for point in EGRESS_POINTS.values():
            with self.subTest(point=point.id):
                self.assertTrue(point.provider)
                self.assertTrue(point.data_category)

    def test_every_phi_point_requires_consent(self):
        for point in EGRESS_POINTS.values():
            with self.subTest(point=point.id):
                if point.phi:
                    self.assertTrue(
                        point.requires_consent,
                        f'{point.id} carries PHI but requires no consent',
                    )

    def test_public_knowledge_indexing_needs_no_consent(self):
        point = EGRESS_POINTS['knowledge.embed']
        self.assertFalse(point.phi)
        self.assertFalse(point.patient_specific)
        self.assertFalse(point.requires_consent)

    def test_previously_identified_paths_are_all_registered(self):
        for point_id in ('rag.generation', 'rag.embed_documents', 'rag.rerank',
                         'rag.classify', 'records.parse', 'records.ocr',
                         'insights.interpretation', 'insights.seizure_proxy'):
            self.assertIn(point_id, EGRESS_POINTS)

    def test_unregistered_point_fails_loudly(self):
        with self.assertRaises(ValueError):
            ExternalProcessingGuard.check(None, 'not.a.real.point')

    def test_matrix_reports_enforcement_state(self):
        with override_settings(CONSENT_ENFORCED_EGRESS=['all']):
            rows = {r['id']: r for r in egress_matrix()}
        self.assertTrue(rows['records.ocr']['enforced'])
        self.assertFalse(rows['knowledge.embed']['enforced'])


class GuardSemanticsTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='eg', email='eg@example.com', password='pw-egress-1',
        )

    @ENFORCE_ALL
    def test_missing_consent_denies(self):
        self.assertFalse(ExternalProcessingGuard.allows(self.user, 'records.ocr'))
        with self.assertRaises(ConsentRequired):
            ExternalProcessingGuard.check(self.user, 'records.ocr')

    @ENFORCE_ALL
    def test_granted_consent_allows(self):
        grant_consent(self.user, EXTERNAL)
        self.assertTrue(ExternalProcessingGuard.allows(self.user, 'records.ocr'))

    @ENFORCE_ALL
    def test_revoked_consent_denies(self):
        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        self.assertFalse(ExternalProcessingGuard.allows(self.user, 'records.ocr'))

    @ENFORCE_ALL
    def test_stale_version_denies(self):
        grant_consent(self.user, EXTERNAL)
        with override_settings(CONSENT_VERSIONS=STALE_VERSION):
            self.assertFalse(ExternalProcessingGuard.allows(self.user, 'records.ocr'))

    @ENFORCE_ALL
    def test_anonymous_user_denied(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(ExternalProcessingGuard.allows(AnonymousUser(), 'records.ocr'))
        self.assertFalse(ExternalProcessingGuard.allows(None, 'records.ocr'))

    @ENFORCE_ALL
    def test_non_phi_point_allowed_without_consent(self):
        self.assertTrue(ExternalProcessingGuard.allows(self.user, 'knowledge.embed'))
        self.assertTrue(ExternalProcessingGuard.allows(None, 'knowledge.embed'))

    def test_unenforced_point_allows_by_default(self):
        """Production default stages the rollout; the point still exists."""
        with override_settings(CONSENT_ENFORCED_EGRESS=['rag']):
            self.assertTrue(ExternalProcessingGuard.allows(self.user, 'records.ocr'))
            self.assertFalse(ExternalProcessingGuard.allows(self.user, 'rag.rerank'))

    def test_rag_shorthand_does_not_cover_the_upload_path(self):
        """
        `rag.embed_documents` is reached by uploading, not by asking, so it was
        never behind the RAGService gate. The 'rag' shorthand must not silently
        start blocking uploads — that is a production change, not a default.
        """
        with override_settings(CONSENT_ENFORCED_EGRESS=['rag']):
            self.assertFalse(ExternalProcessingGuard.is_enforced('rag.embed_documents'))
            self.assertTrue(ExternalProcessingGuard.allows(self.user, 'rag.embed_documents'))
            self.assertTrue(ExternalProcessingGuard.is_enforced('rag.generation'))

    def test_explicit_point_list_is_honoured(self):
        with override_settings(CONSENT_ENFORCED_EGRESS=['records.ocr']):
            self.assertFalse(ExternalProcessingGuard.allows(self.user, 'records.ocr'))
            self.assertTrue(ExternalProcessingGuard.allows(self.user, 'records.parse'))


@ENFORCE_ALL
class DocumentIngestionEgressTests(TestCase):
    """
    Uploads are the path the RAG gate never saw: indexing and parsing are
    triggered by saving a record, not by asking a question.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='doc', email='doc@example.com', password='pw-doc-egress-1',
        )

    # ── OCR ───────────────────────────────────────────────────────────────────

    def _ocr(self):
        from apps.medical_records.services import MedicalRecordService
        return MedicalRecordService.ocr_image(b'\xff\xd8\xff-fake-jpeg', user=self.user)

    def test_ocr_without_consent_never_calls_gemini(self):
        genai = MagicMock()
        with patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            result = self._ocr()
        genai.Client.assert_not_called()
        self.assertEqual(result['consent_required'], EXTERNAL)

    def test_ocr_with_revoked_consent_never_calls_gemini(self):
        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        genai = MagicMock()
        with patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            result = self._ocr()
        genai.Client.assert_not_called()
        self.assertIn('consent_required', result)

    def test_ocr_with_stale_consent_never_calls_gemini(self):
        grant_consent(self.user, EXTERNAL)
        genai = MagicMock()
        with override_settings(CONSENT_VERSIONS=STALE_VERSION), \
             patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            result = self._ocr()
        genai.Client.assert_not_called()
        self.assertIn('consent_required', result)

    def test_ocr_with_consent_reaches_the_provider(self):
        grant_consent(self.user, EXTERNAL)
        genai = MagicMock()
        with patch.dict('sys.modules', {'google.genai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            result = self._ocr()
        # With consent the guard steps aside; whatever happens next is the
        # provider's business, not a consent refusal.
        self.assertNotIn('consent_required', result)

    # ── LLM document parsing ──────────────────────────────────────────────────

    def test_text_parsing_without_consent_never_calls_gemini(self):
        from apps.medical_records.services import MedicalRecordService

        with patch('apps.medical_records.parsers._structure_medical_text_with_ai',
                   wraps=_spy_structure) as spy:
            MedicalRecordService.create_from_text(self.user, 'CREATININE 250 umol/L')
        self.assertFalse(spy.call_args.kwargs.get('allow_external', True),
                         'document text was offered to the external parser')

    def test_text_parsing_with_consent_permits_external(self):
        from apps.medical_records.services import MedicalRecordService

        grant_consent(self.user, EXTERNAL)
        with patch('apps.medical_records.parsers._structure_medical_text_with_ai',
                   wraps=_spy_structure) as spy:
            MedicalRecordService.create_from_text(self.user, 'CREATININE 250 umol/L')
        self.assertTrue(spy.call_args.kwargs.get('allow_external'))

    def test_parser_without_permission_does_not_construct_a_gemini_client(self):
        from apps.medical_records.parsers import _structure_medical_text_with_ai

        genai = MagicMock()
        with patch.dict('sys.modules', {'google.generativeai': genai}), \
             self.settings(GEMINI_API_KEY='test-key'):
            _structure_medical_text_with_ai('CREATININE 250', allow_external=False)
        genai.configure.assert_not_called()
        genai.GenerativeModel.assert_not_called()

    def test_local_extraction_still_works_without_consent(self):
        """Refusing external processing must not break ingestion entirely."""
        from apps.medical_records.models import MedicalRecord
        from apps.medical_records.services import MedicalRecordService

        result = MedicalRecordService.create_from_text(
            self.user, 'CREATININE 250 umol/L\nGLUCOSE 5.4 mmol/L',
        )
        self.assertTrue(MedicalRecord.objects.filter(pk=result['record'].pk).exists())

    # ── Embedding on upload ───────────────────────────────────────────────────

    def test_embedding_upload_without_consent_never_calls_the_api(self):
        from apps.rag_assistant.models import MedicalChunk, MedicalDocument
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        doc = MedicalDocument.objects.create(
            patient=self.user, title='D', document_type='raw_text', content='c',
        )
        chunk = MedicalChunk.objects.create(
            document=doc, patient=self.user, content='CREATININE 250', chunk_index=0,
        )
        with patch.object(EmbeddingService, '_call_api') as spy:
            EmbeddingService().embed_chunks([chunk])
        spy.assert_not_called()
        chunk.refresh_from_db()
        self.assertIsNone(chunk.embedding)

    def test_embedding_upload_with_consent_calls_the_api(self):
        import numpy as np
        from apps.rag_assistant.models import MedicalChunk, MedicalDocument
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        grant_consent(self.user, EXTERNAL)
        doc = MedicalDocument.objects.create(
            patient=self.user, title='D', document_type='raw_text', content='c',
        )
        chunk = MedicalChunk.objects.create(
            document=doc, patient=self.user, content='CREATININE 250', chunk_index=0,
        )
        with patch.object(EmbeddingService, '_call_api',
                          return_value=np.ones((1, 3072), dtype=np.float32)) as spy:
            EmbeddingService().embed_chunks([chunk])
        spy.assert_called_once()

    def test_query_embedding_without_consent_never_calls_the_api(self):
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        with patch.object(EmbeddingService, '_call_api') as spy:
            with self.assertRaises(ConsentRequired):
                EmbeddingService().embed('what is my creatinine?', user=self.user)
        spy.assert_not_called()

    def test_knowledge_base_embedding_is_not_blocked(self):
        """Public article indexing has no patient and must keep working."""
        import numpy as np
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        with patch.object(EmbeddingService, '_call_api',
                          return_value=np.ones((1, 3072), dtype=np.float32)) as spy:
            EmbeddingService().embed('what is hypertension?')
        spy.assert_called_once()


@ENFORCE_ALL
class RagInternalEgressTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='ri', email='ri@example.com', password='pw-rag-egress-1',
        )

    def test_reranker_without_consent_never_calls_groq(self):
        from apps.rag_assistant.services.retrieval_service import RetrievalService

        svc = RetrievalService.__new__(RetrievalService)
        candidates = [{'text': f'chunk {i}', 'score': 1.0, 'metadata': {}} for i in range(5)]

        groq = MagicMock()
        with patch.dict('sys.modules', {'groq': groq}), \
             self.settings(GROQ_API_KEY='test-key'):
            out = svc._llm_rerank('q', candidates, top_k=2, patient=self.user)
        groq.Groq.assert_not_called()
        self.assertEqual(len(out), 2, 'Stage-1 ordering must still be returned')

    def test_reranker_with_consent_calls_groq(self):
        from apps.rag_assistant.services.retrieval_service import RetrievalService

        grant_consent(self.user, EXTERNAL)
        svc = RetrievalService.__new__(RetrievalService)
        candidates = [{'text': f'chunk {i}', 'score': 1.0, 'metadata': {}} for i in range(5)]

        groq = MagicMock()
        groq.Groq.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='[1,2]'))]
        )
        with patch.dict('sys.modules', {'groq': groq}), \
             self.settings(GROQ_API_KEY='test-key'):
            svc._llm_rerank('q', candidates, top_k=2, patient=self.user)
        groq.Groq.assert_called_once()

    def test_classifier_without_consent_never_calls_groq(self):
        from apps.rag_assistant.services.query_understanding import _llm_classify

        groq = MagicMock()
        with patch.dict('sys.modules', {'groq': groq}), \
             self.settings(GROQ_API_KEY='test-key'):
            result = _llm_classify('is it better?', [], user=self.user)
        groq.Groq.assert_not_called()
        self.assertIsNone(result, 'caller must fall back to the local classifier')


@ENFORCE_ALL
class PredictionEgressTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='pi', email='pi@example.com', password='pw-pred-egress-1',
        )
        from apps.ai_insights.models import AIModel
        self.model = AIModel.objects.create(
            data_scientist=self.user, name='Cardio Risk', description='d',
            status='approved',
        )
        self.model.status = 'active'
        self.model.save(update_fields=['status'])

    def _interpret(self):
        from apps.ai_insights.inference.interpretation import generate_interpretation
        return generate_interpretation(
            self.model, {'label': 'high risk', 'risk_score': 0.9},
            {'age': 61, 'cholesterol': 7.2}, user=self.user,
        )

    def test_interpretation_without_consent_never_calls_a_provider(self):
        groq, genai = MagicMock(), MagicMock()
        with patch.dict('sys.modules', {'groq': groq, 'google.genai': genai}), \
             self.settings(GROQ_API_KEY='k', GEMINI_API_KEY='k'):
            text = self._interpret()
        groq.Groq.assert_not_called()
        genai.Client.assert_not_called()
        self.assertTrue(text, 'the static interpretation must still be returned')

    def test_interpretation_with_consent_calls_the_provider(self):
        grant_consent(self.user, EXTERNAL)
        groq = MagicMock()
        groq.Groq.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='Your result means...'))]
        )
        with patch.dict('sys.modules', {'groq': groq}), \
             self.settings(GROQ_API_KEY='k'):
            self._interpret()
        groq.Groq.assert_called_once()

    def test_revoked_consent_falls_back_to_static(self):
        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        groq = MagicMock()
        with patch.dict('sys.modules', {'groq': groq}), self.settings(GROQ_API_KEY='k'):
            self._interpret()
        groq.Groq.assert_not_called()


@ENFORCE_ALL
class SeizureProxyEgressTests(TestCase):
    """
    hasanai.net receives the raw EEG file. It is a third party outside this
    system, the request carries no authentication, and retention there is
    undocumented — so the file must not leave without consent.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='sz', email='sz@example.com', password='pw-seizure-1',
        )
        self.client.force_login(self.user)

    def _upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile('eeg.parquet', b'PAR1-fake-eeg-bytes',
                                  content_type='application/octet-stream')

    def test_web_proxy_without_consent_makes_no_outbound_request(self):
        with patch('requests.post') as spy:
            resp = self.client.post('/insights/seizure/', {'signal_file': self._upload()})
        spy.assert_not_called()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['consent_required'], EXTERNAL)

    def test_web_proxy_with_consent_makes_the_request(self):
        grant_consent(self.user, EXTERNAL)
        with patch('requests.post') as spy:
            spy.return_value = MagicMock(
                status_code=200, json=lambda: {'ensemble_label': 'Seizure'},
                raise_for_status=lambda: None,
            )
            self.client.post('/insights/seizure/', {'signal_file': self._upload()})
        spy.assert_called_once()

    def test_api_proxy_without_consent_makes_no_outbound_request(self):
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(user=self.user)
        with patch('requests.post') as spy:
            resp = api.post('/api/v1/seizure-analysis/',
                            {'signal_file': self._upload()}, format='multipart')
        spy.assert_not_called()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['consent_required'], EXTERNAL)

    def test_api_proxy_with_stale_consent_makes_no_outbound_request(self):
        from rest_framework.test import APIClient

        grant_consent(self.user, EXTERNAL)
        api = APIClient()
        api.force_authenticate(user=self.user)
        with override_settings(CONSENT_VERSIONS=STALE_VERSION), patch('requests.post') as spy:
            resp = api.post('/api/v1/seizure-analysis/',
                            {'signal_file': self._upload()}, format='multipart')
        spy.assert_not_called()
        self.assertEqual(resp.status_code, 403)


def _spy_structure(text, table_lab_values=None, allow_external=True):
    """Stand-in for the real structurer that records how it was called."""
    return None

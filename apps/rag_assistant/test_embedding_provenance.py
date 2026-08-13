"""
Tests for per-chunk embedding provenance and compatibility enforcement.

The failure this guards against: a stored vector and a query vector from
different embedding models still produce a cosine number, so an incompatible
index degrades silently rather than erroring. Every test here asserts that
incompatibility is *detected* and the affected rows are *excluded*, never blended.

Covers:
  classify_embedding      — model / dimension / strict-mode decision table
  embed_chunks            — provenance stamped on every new vector
  audit_embeddings        — staleness detection
  load_patient_embeddings — incompatible rows excluded from retrieval
  index_status            — staleness surfaced to callers
"""
import django.test
import numpy as np
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from apps.rag_assistant.models import MedicalChunk, MedicalDocument
from apps.rag_assistant.services.embedding_service import (
    COMPAT_DIM_MISMATCH,
    COMPAT_MODEL_MISMATCH,
    COMPAT_OK,
    COMPAT_UNKNOWN,
    EmbeddingService,
    active_embedding_model,
    audit_embeddings,
    classify_embedding,
)

ACTIVE_MODEL = settings.RAG_CONFIG['EMBEDDING_MODEL']
ACTIVE_DIM   = settings.RAG_CONFIG['EMBEDDING_DIM']


def _swapped_config(model='models/brand-new-embedding'):
    return {**settings.RAG_CONFIG, 'EMBEDDING_MODEL': model}


# ── Decision table ────────────────────────────────────────────────────────────

class ClassifyEmbeddingTests(SimpleTestCase):
    """classify_embedding() is the single gate deciding whether a vector is usable."""

    def _classify(self, model, dim, actual=None, strict=False):
        return classify_embedding(
            model, dim, actual,
            active_model=ACTIVE_MODEL, active_dim=ACTIVE_DIM, strict=strict,
        )

    def test_matching_model_and_dim_is_ok(self):
        self.assertEqual(self._classify(ACTIVE_MODEL, ACTIVE_DIM), COMPAT_OK)

    def test_different_model_same_dim_is_detected(self):
        """The dangerous case: identical dimensions, different vector space."""
        self.assertEqual(
            self._classify('models/other-3072-model', ACTIVE_DIM), COMPAT_MODEL_MISMATCH,
        )

    def test_wrong_dimension_is_detected(self):
        self.assertEqual(self._classify(ACTIVE_MODEL, 768), COMPAT_DIM_MISMATCH)

    def test_legacy_row_usable_by_default(self):
        """Pre-provenance rows of the right size keep working — no forced re-embed."""
        self.assertEqual(self._classify('', ACTIVE_DIM), COMPAT_OK)

    def test_legacy_row_rejected_under_strict(self):
        self.assertEqual(self._classify('', ACTIVE_DIM, strict=True), COMPAT_UNKNOWN)

    def test_legacy_row_with_wrong_dim_rejected_in_both_modes(self):
        self.assertEqual(self._classify('', 768), COMPAT_DIM_MISMATCH)
        self.assertEqual(self._classify('', 768, strict=True), COMPAT_DIM_MISMATCH)

    def test_actual_dim_overrides_recorded_dim(self):
        """Bytes on disk are ground truth; the column is only an annotation."""
        self.assertEqual(
            self._classify(ACTIVE_MODEL, ACTIVE_DIM, actual=768), COMPAT_DIM_MISMATCH,
        )

    def test_non_string_model_treated_as_unrecorded(self):
        self.assertEqual(self._classify(None, ACTIVE_DIM), COMPAT_OK)

    def test_missing_model_config_raises_rather_than_defaulting(self):
        """A blank config must fail loudly, not fall back to a deprecated model."""
        with override_settings(RAG_CONFIG={**settings.RAG_CONFIG, 'EMBEDDING_MODEL': ''}):
            with self.assertRaises(ImproperlyConfigured):
                active_embedding_model()


# ── Shared fixture ────────────────────────────────────────────────────────────

class _ChunkFixtureMixin:

    def _make_user(self, name):
        return get_user_model().objects.create_user(
            username=name, password='pw-test-only', email=f'{name}@example.com',
        )

    def _make_doc(self, user):
        return MedicalDocument.objects.create(
            patient=user, title='Doc', document_type='raw_text', content='c',
        )

    def _make_chunk(self, doc, user, idx, *, content=None, model=None, dim=None):
        kwargs = {}
        if dim is not None:
            kwargs['embedding']            = np.ones(dim, dtype=np.float32).tobytes()
            kwargs['embedding_dimensions'] = dim
        if model is not None:
            kwargs['embedding_model'] = model
        return MedicalChunk.objects.create(
            document=doc, patient=user, chunk_index=idx,
            content=content or f'chunk {idx}', **kwargs,
        )


# ── Write path ────────────────────────────────────────────────────────────────

class EmbedChunksProvenanceTests(_ChunkFixtureMixin, django.test.TestCase):
    """embed_chunks() must record provenance on every vector it writes."""

    def setUp(self):
        self.user  = self._make_user('prov-user')
        self.doc   = self._make_doc(self.user)
        self.chunk = self._make_chunk(self.doc, self.user, 0, content='creatinine 95')

    def test_row_created_before_backfill_has_empty_provenance(self):
        """The migration is additive: pre-existing rows survive with blank provenance."""
        self.assertEqual(self.chunk.embedding_model, '')
        self.assertEqual(self.chunk.embedding_model_version, '')
        self.assertIsNone(self.chunk.embedding_dimensions)
        self.assertIsNone(self.chunk.embedded_at)

    def test_embed_chunks_stamps_model_dim_and_time(self):
        from unittest.mock import patch

        vec = np.ones((1, ACTIVE_DIM), dtype=np.float32)
        with patch.object(EmbeddingService, '_call_api', return_value=vec):
            EmbeddingService().embed_chunks([self.chunk])

        stored = MedicalChunk.objects.get(pk=self.chunk.pk)
        self.assertEqual(stored.embedding_model, ACTIVE_MODEL)
        self.assertEqual(stored.embedding_dimensions, ACTIVE_DIM)
        self.assertIsNotNone(stored.embedded_at)
        self.assertIsNotNone(stored.embedding)

    def test_newly_stamped_chunk_is_immediately_retrievable(self):
        from unittest.mock import patch

        vec = np.ones((1, ACTIVE_DIM), dtype=np.float32)
        with patch.object(EmbeddingService, '_call_api', return_value=vec):
            EmbeddingService().embed_chunks([self.chunk])

        texts, matrix, _ = EmbeddingService().load_patient_embeddings(self.user)
        self.assertEqual(texts, ['creatinine 95'])
        self.assertEqual(matrix.shape, (1, ACTIVE_DIM))

    def test_api_failure_leaves_existing_vector_and_provenance_intact(self):
        from unittest.mock import patch

        vec = np.ones((1, ACTIVE_DIM), dtype=np.float32)
        with patch.object(EmbeddingService, '_call_api', return_value=vec):
            EmbeddingService().embed_chunks([self.chunk])
        before = MedicalChunk.objects.get(pk=self.chunk.pk)

        with patch.object(EmbeddingService, '_call_api', side_effect=RuntimeError('API down')):
            EmbeddingService().embed_chunks([MedicalChunk.objects.get(pk=self.chunk.pk)])

        after = MedicalChunk.objects.get(pk=self.chunk.pk)
        self.assertEqual(bytes(after.embedding), bytes(before.embedding))
        self.assertEqual(after.embedding_model, before.embedding_model)
        self.assertEqual(after.embedded_at, before.embedded_at)


# ── Staleness detection ───────────────────────────────────────────────────────

class AuditEmbeddingsTests(_ChunkFixtureMixin, django.test.TestCase):
    """audit_embeddings() is the mechanism for identifying stale rows."""

    def setUp(self):
        self.user = self._make_user('audit-user')
        doc = self._make_doc(self.user)
        self._make_chunk(doc, self.user, 0, model=ACTIVE_MODEL, dim=ACTIVE_DIM)
        self._make_chunk(doc, self.user, 1, model=ACTIVE_MODEL, dim=ACTIVE_DIM)
        self._make_chunk(doc, self.user, 2, model='models/text-embedding-004', dim=768)
        self._make_chunk(doc, self.user, 3, model='', dim=ACTIVE_DIM)   # legacy
        self._make_chunk(doc, self.user, 4)                             # never embedded

    def test_counts_compatible_stale_and_unembedded(self):
        rep = audit_embeddings(MedicalChunk)
        self.assertEqual(rep['total'], 5)
        self.assertEqual(rep['unembedded'], 1)
        self.assertEqual(rep['compatible'], 3)   # legacy row usable in non-strict mode
        self.assertEqual(rep['stale'], 1)

    def test_breakdown_groups_by_model_and_dimension(self):
        rep = audit_embeddings(MedicalChunk)
        by_model = {r['embedding_model']: r for r in rep['breakdown']}
        self.assertEqual(by_model[ACTIVE_MODEL]['count'], 2)
        self.assertEqual(by_model['models/text-embedding-004']['status'], COMPAT_DIM_MISMATCH)
        self.assertEqual(by_model['(unrecorded)']['status'], COMPAT_OK)

    def test_strict_mode_flags_unknown_provenance_as_stale(self):
        with override_settings(EMBEDDING_STRICT_PROVENANCE=True):
            rep = audit_embeddings(MedicalChunk)
        self.assertEqual(rep['compatible'], 2)
        self.assertEqual(rep['stale'], 2)

    def test_model_swap_marks_stamped_rows_stale(self):
        """Changing the model must invalidate vectors from the previous one."""
        with override_settings(RAG_CONFIG=_swapped_config()):
            rep = audit_embeddings(MedicalChunk)
        self.assertEqual(rep['stale'], 3)
        self.assertEqual(rep['compatible'], 1)   # only the unstamped legacy row

    def test_index_status_surfaces_stale_count(self):
        from apps.rag_assistant.services.rag_service import RAGService

        status = RAGService().index_status(self.user)
        self.assertEqual(status['embedded_chunks'], 4)
        self.assertEqual(status['usable_chunks'], 3)
        self.assertEqual(status['stale_chunks'], 1)
        self.assertEqual(status['embedding_model'], ACTIVE_MODEL)


# ── Read path ─────────────────────────────────────────────────────────────────

class RetrievalCompatibilityTests(_ChunkFixtureMixin, django.test.TestCase):
    """Incompatible vectors must be excluded from retrieval, never blended in."""

    def setUp(self):
        self.user = self._make_user('retr-user')
        doc = self._make_doc(self.user)
        self._make_chunk(doc, self.user, 0, content='good chunk',
                         model=ACTIVE_MODEL, dim=ACTIVE_DIM)
        self._make_chunk(doc, self.user, 1, content='old model chunk',
                         model='models/text-embedding-004', dim=ACTIVE_DIM)
        self._make_chunk(doc, self.user, 2, content='wrong dim chunk',
                         model=ACTIVE_MODEL, dim=768)

    def test_only_compatible_chunks_are_loaded(self):
        texts, matrix, meta = EmbeddingService().load_patient_embeddings(self.user)
        self.assertEqual(texts, ['good chunk'])
        self.assertEqual(matrix.shape, (1, ACTIVE_DIM))
        self.assertEqual(len(meta), 1)

    def test_same_dim_different_model_is_excluded(self):
        """'old model chunk' has the right shape — only provenance reveals it is wrong."""
        texts, _, _ = EmbeddingService().load_patient_embeddings(self.user)
        self.assertNotIn('old model chunk', texts)

    def test_model_swap_empties_index_rather_than_mixing_spaces(self):
        with override_settings(RAG_CONFIG=_swapped_config()):
            texts, matrix, meta = EmbeddingService().load_patient_embeddings(self.user)

        self.assertEqual(texts, [])
        self.assertEqual(matrix.shape, (0, ACTIVE_DIM))
        self.assertEqual(meta, [])

    def test_strict_mode_excludes_legacy_rows(self):
        doc = MedicalDocument.objects.get(patient=self.user)
        self._make_chunk(doc, self.user, 3, content='legacy chunk', model='', dim=ACTIVE_DIM)

        texts, _, _ = EmbeddingService().load_patient_embeddings(self.user)
        self.assertIn('legacy chunk', texts)

        with override_settings(EMBEDDING_STRICT_PROVENANCE=True):
            strict_texts, _, _ = EmbeddingService().load_patient_embeddings(self.user)
        self.assertNotIn('legacy chunk', strict_texts)
        self.assertEqual(strict_texts, ['good chunk'])

"""
REGRESSION — CB-2: an embedding failure must never be a silent, permanent loss.

Before this, `embed_chunks()` caught every exception, logged it and returned.
The chunks stayed in the database with `embedding = NULL`, retrieval excluded
them via `embedding__isnull=False`, and the patient still saw the record in
their list — so the assistant denied the existence of a document that was
plainly there. Nothing retried, nothing alerted, and NULL could not be told
apart from "not attempted yet" or "consent refused".

The invariant these tests pin:

    a medical document must never silently appear as successfully indexed while
    being permanently invisible to retrieval because its embedding failed.

Covered failure modes: quota/API error, transient network error, invalid
provider response (short batch, zero vectors), partially successful multi-batch
runs, and the state that survives a process restart.

Every test patches the provider — no network, no quota, no MIMIC data.
"""
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.rag_assistant.models import MedicalChunk, MedicalDocument
from apps.rag_assistant.services.embedding_service import (
    EmbeddingService, active_embedding_dim,
)

Status = MedicalChunk.EmbeddingStatus
DIM = active_embedding_dim()


def _good(n):
    """n usable vectors."""
    return np.ones((n, DIM), dtype=np.float32)


class _Fixture(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='cb2', password='pw-test-only', email='cb2@example.com')
        self.doc = MedicalDocument.objects.create(
            patient=self.user, title='Doc', document_type='lab_result', content='c')
        self.svc = EmbeddingService()

    def _chunks(self, n=3):
        return [
            MedicalChunk.objects.create(
                document=self.doc, patient=self.user, chunk_index=i,
                content=f'chunk {i}')
            for i in range(n)
        ]

    def _allow(self):
        return patch('apps.accounts.egress.ExternalProcessingGuard.allows',
                     return_value=True)


class InitialStateTests(_Fixture):

    def test_new_chunk_is_pending_not_silently_complete(self):
        """A freshly created chunk must be distinguishable from a finished one."""
        chunk = self._chunks(1)[0]
        self.assertEqual(chunk.embedding_status, Status.PENDING)
        self.assertIsNone(chunk.embedding)


class FailureModeTests(_Fixture):
    """Each mode must leave a retryable, observable state — never silence."""

    def _run_with_error(self, exc):
        chunks = self._chunks(3)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch', side_effect=exc):
            self.svc.embed_chunks(chunks)
        return [MedicalChunk.objects.get(pk=c.pk) for c in chunks]

    def test_quota_failure_is_recorded_as_failed(self):
        """Mode 1. Was: logged and forgotten, chunk indistinguishable from new."""
        rows = self._run_with_error(RuntimeError('429 RESOURCE_EXHAUSTED quota exceeded'))
        for r in rows:
            self.assertEqual(r.embedding_status, Status.FAILED)
            self.assertIn('429', r.embedding_error)
            self.assertEqual(r.embedding_attempts, 1)
            self.assertIsNotNone(r.embedding_attempted_at)
            self.assertIsNone(r.embedding)          # no fabricated vector

    def test_transient_network_failure_is_recorded_as_failed(self):
        """Mode 2."""
        rows = self._run_with_error(ConnectionError('connection reset'))
        self.assertTrue(all(r.embedding_status == Status.FAILED for r in rows))

    def test_invalid_response_short_batch_marks_the_remainder_failed(self):
        """
        Mode 3. The provider returns fewer vectors than requested. zip() used to
        drop the surplus silently, leaving those chunks NULL with no error.
        """
        chunks = self._chunks(3)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         return_value=_good(2)):
            self.svc.embed_chunks(chunks)
        rows = [MedicalChunk.objects.get(pk=c.pk) for c in chunks]
        self.assertEqual(rows[0].embedding_status, Status.EMBEDDED)
        self.assertEqual(rows[1].embedding_status, Status.EMBEDDED)
        self.assertEqual(rows[2].embedding_status, Status.FAILED)
        self.assertIn('no usable vector', rows[2].embedding_error)

    def test_zero_vectors_are_failed_not_silently_skipped(self):
        """Mode 3b. `if np.any(vec)` skipped these without recording anything."""
        chunks = self._chunks(2)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         return_value=np.zeros((2, DIM), dtype=np.float32)):
            self.svc.embed_chunks(chunks)
        rows = [MedicalChunk.objects.get(pk=c.pk) for c in chunks]
        self.assertTrue(all(r.embedding_status == Status.FAILED for r in rows))
        self.assertTrue(all(r.embedding is None for r in rows))

    def test_consent_refusal_is_blocked_not_failed(self):
        """Refusing to transmit is the system working, and must not look broken."""
        chunks = self._chunks(2)
        with patch('apps.accounts.egress.ExternalProcessingGuard.allows',
                   return_value=False):
            self.svc.embed_chunks(chunks)
        rows = [MedicalChunk.objects.get(pk=c.pk) for c in chunks]
        self.assertTrue(all(r.embedding_status == Status.BLOCKED for r in rows))


class PartialBatchTests(_Fixture):
    """Mode 4 — successful work must survive a later failure."""

    def test_first_batch_survives_when_a_later_batch_fails(self):
        """
        Was: a single embed_batch() call covered every chunk, so one failure
        discarded vectors already computed — and the quota spent on them.
        """
        chunks = self._chunks(150)          # > _BATCH_LIMIT (100)
        calls = {'n': 0}

        def flaky(texts, task_type='RETRIEVAL_DOCUMENT'):
            calls['n'] += 1
            if calls['n'] == 1:
                return _good(len(texts))
            raise RuntimeError('429 quota exhausted mid-run')

        with self._allow(), patch.object(EmbeddingService, 'embed_batch', side_effect=flaky):
            self.svc.embed_chunks(chunks)

        embedded = MedicalChunk.objects.filter(
            document=self.doc, embedding_status=Status.EMBEDDED).count()
        failed = MedicalChunk.objects.filter(
            document=self.doc, embedding_status=Status.FAILED).count()
        self.assertEqual(embedded, 100)     # first batch preserved
        self.assertEqual(failed, 50)
        self.assertEqual(embedded + failed, 150)


class RestartAndRecoveryTests(_Fixture):
    """Mode 5 — state persists, and recovery is idempotent and in place."""

    def _fail_then_recover(self):
        chunks = self._chunks(3)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=RuntimeError('quota')):
            self.svc.embed_chunks(chunks)
        return chunks

    def test_failed_state_survives_a_restart(self):
        """State is in the database, not in an in-process queue that is lost."""
        chunks = self._fail_then_recover()
        reloaded = MedicalChunk.objects.filter(pk__in=[c.pk for c in chunks])
        self.assertEqual(reloaded.filter(embedding_status=Status.FAILED).count(), 3)

    def test_retry_command_recovers_failed_chunks_in_place(self):
        """ACCEPTANCE — CB-2. The recovery path that did not exist."""
        chunks = self._fail_then_recover()
        ids_before = sorted(str(c.pk) for c in chunks)

        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=lambda t, **k: _good(len(t))):
            call_command('retry_failed_embeddings', verbosity=0)

        rows = MedicalChunk.objects.filter(pk__in=[c.pk for c in chunks])
        self.assertEqual(rows.filter(embedding_status=Status.EMBEDDED).count(), 3)
        self.assertTrue(all(r.embedding is not None for r in rows))
        # In place: same rows, no delete/recreate, so ids are stable.
        self.assertEqual(sorted(str(r.pk) for r in rows), ids_before)

    def test_retry_creates_no_duplicate_chunks_or_documents(self):
        chunks = self._fail_then_recover()
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=lambda t, **k: _good(len(t))):
            call_command('retry_failed_embeddings', verbosity=0)
        self.assertEqual(MedicalChunk.objects.filter(document=self.doc).count(), len(chunks))
        self.assertEqual(MedicalDocument.objects.filter(patient=self.user).count(), 1)

    def test_retry_is_idempotent(self):
        """Running it twice must embed nothing the second time."""
        self._fail_then_recover()
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=lambda t, **k: _good(len(t))):
            call_command('retry_failed_embeddings', verbosity=0)

        with self._allow(), patch.object(EmbeddingService, 'embed_batch') as spy:
            spy.side_effect = lambda t, **k: _good(len(t))
            call_command('retry_failed_embeddings', verbosity=0)
        spy.assert_not_called()

    def test_retry_preserves_already_successful_embeddings(self):
        """A successful vector must never be recomputed or overwritten."""
        chunks = self._chunks(2)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=lambda t, **k: _good(len(t))):
            self.svc.embed_chunks(chunks)
        first = MedicalChunk.objects.get(pk=chunks[0].pk)
        stamp = first.embedded_at

        with self._allow(), patch.object(EmbeddingService, 'embed_batch') as spy:
            call_command('retry_failed_embeddings', verbosity=0)
        spy.assert_not_called()
        self.assertEqual(MedicalChunk.objects.get(pk=chunks[0].pk).embedded_at, stamp)

    def test_retry_skips_consent_blocked_chunks_by_default(self):
        """A retry must not push data the patient declined to share."""
        chunks = self._chunks(2)
        with patch('apps.accounts.egress.ExternalProcessingGuard.allows', return_value=False):
            self.svc.embed_chunks(chunks)

        with patch.object(EmbeddingService, 'embed_batch') as spy:
            call_command('retry_failed_embeddings', verbosity=0)
        spy.assert_not_called()
        rows = MedicalChunk.objects.filter(pk__in=[c.pk for c in chunks])
        self.assertEqual(rows.filter(embedding_status=Status.BLOCKED).count(), 2)

    def test_retry_never_fabricates_a_vector_when_the_provider_still_fails(self):
        chunks = self._fail_then_recover()
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=RuntimeError('still down')):
            call_command('retry_failed_embeddings', verbosity=0)
        rows = MedicalChunk.objects.filter(pk__in=[c.pk for c in chunks])
        self.assertTrue(all(r.embedding is None for r in rows))
        self.assertTrue(all(r.embedding_status == Status.FAILED for r in rows))
        self.assertTrue(all(r.embedding_attempts >= 2 for r in rows))


class RetrievalEligibilityTests(_Fixture):
    """Unembedded chunks stay out of retrieval — but no longer in silence."""

    def test_failed_chunks_are_not_returned_as_indexed(self):
        chunks = self._chunks(2)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=RuntimeError('quota')):
            self.svc.embed_chunks(chunks)
        texts, matrix, meta = self.svc.load_patient_embeddings(self.user)
        self.assertEqual(texts, [])
        self.assertEqual(len(meta), 0)

    def test_exclusion_is_logged_rather_than_silent(self):
        """The signal that was entirely missing before."""
        chunks = self._chunks(2)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=RuntimeError('quota')):
            self.svc.embed_chunks(chunks)
        with self.assertLogs('apps.rag_assistant.services.embedding_service',
                             level='WARNING') as logs:
            self.svc.load_patient_embeddings(self.user)
        self.assertTrue(any('not retrievable' in m for m in logs.output))

    def test_recovered_chunks_become_retrievable(self):
        """End to end: failure -> recovery -> visible to retrieval."""
        chunks = self._chunks(2)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=RuntimeError('quota')):
            self.svc.embed_chunks(chunks)
        self.assertEqual(self.svc.load_patient_embeddings(self.user)[0], [])

        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=lambda t, **k: _good(len(t))):
            call_command('retry_failed_embeddings', verbosity=0)

        texts, _matrix, _meta = self.svc.load_patient_embeddings(self.user)
        self.assertEqual(len(texts), 2)


class ObservabilityTests(_Fixture):
    """
    coverage_pct alone cannot distinguish a retryable failure from a patient who
    declined external processing. index_status must report the cause.
    """

    def test_index_status_reports_failed_chunks(self):
        chunks = self._chunks(2)
        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=RuntimeError('quota')):
            self.svc.embed_chunks(chunks)

        from apps.rag_assistant.services.rag_service import RAGService
        status = RAGService().index_status(self.user)
        self.assertEqual(status['failed_chunks'], 2)
        self.assertEqual(status['blocked_chunks'], 0)
        self.assertEqual(status['embedded_chunks'], 0)

    def test_index_status_distinguishes_blocked_from_failed(self):
        chunks = self._chunks(2)
        with patch('apps.accounts.egress.ExternalProcessingGuard.allows', return_value=False):
            self.svc.embed_chunks(chunks)

        from apps.rag_assistant.services.rag_service import RAGService
        status = RAGService().index_status(self.user)
        self.assertEqual(status['blocked_chunks'], 2)
        self.assertEqual(status['failed_chunks'], 0)


class IsolationTests(_Fixture):
    """Recovery must not cross patient boundaries."""

    def test_retry_groups_by_patient_and_does_not_leak(self):
        other = get_user_model().objects.create_user(
            username='cb2-other', password='pw-test-only', email='o@example.com')
        other_doc = MedicalDocument.objects.create(
            patient=other, title='D', document_type='lab_result', content='c')
        mine = self._chunks(2)
        theirs = MedicalChunk.objects.create(
            document=other_doc, patient=other, chunk_index=0, content='theirs')

        with self._allow(), patch.object(EmbeddingService, 'embed_batch',
                                         side_effect=RuntimeError('quota')):
            self.svc.embed_chunks(mine)

        call_command('retry_failed_embeddings', patient=str(self.user.pk), verbosity=0)
        self.assertEqual(
            MedicalChunk.objects.get(pk=theirs.pk).embedding_status, Status.PENDING)

"""
P0.2 — `indexed_at` did not mean "indexed".

Two defects, both of which made the column say the opposite of the truth in
exactly the cases that matter:

  1. `index_record()` returns how many chunks it CREATED, and chunks are created
     before they are embedded. Embedding can be refused (no consent) or fail
     (provider down), and both leave a positive count — so a record with no
     usable vectors at all was stamped as indexed, and the assistant could not
     find a record the patient could see in their list.

  2. There was no claim. Two workers seeing the same save both indexed and both
     stamped, and a third could read a fresh timestamp while indexing was still
     running.

Every transition here is a compare-and-set against the current status, so the
database decides who wins. These tests exercise the losing side as much as the
winning one, because the losing side is where the old behaviour was wrong.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.medical_records.models import MedicalRecord

User = get_user_model()
S = MedicalRecord.IndexStatus


# The transitions are exercised directly here, so the auto-indexer is switched
# off for this module. Under the test runner RAG_AUTO_INDEX_SYNC is True, which
# means creating a record indexes it inline — consuming a claim and moving the
# state before the test has begun. That behaviour is correct and is covered by
# the pipeline tests below; here it is noise that hides what is being asserted.
@override_settings(RAG_AUTO_INDEX_SYNC=False)
class _Records(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'ix_patient', email='ix@test.invalid', password='pw', role='patient')

    def _record(self, **kwargs):
        # Created with update_fields-free save; the post_save receiver runs and
        # leaves it PENDING, which is the state a new record should be in.
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Discharge summary',
            record_type='discharge', **kwargs)
        record.refresh_from_db()
        return record

    def _reset(self, record, status=S.PENDING):
        """Put the row in a known state, attempts included."""
        MedicalRecord.objects.filter(pk=record.pk).update(
            index_status=status, index_attempts=0, index_error='')
        record.refresh_from_db()
        return record


class ValidTransitionTests(_Records):

    def test_a_new_record_starts_pending(self):
        record = self._reset(self._record())
        self.assertEqual(record.index_status, S.PENDING)
        self.assertFalse(record.is_searchable)

    def test_pending_to_indexing_to_indexed(self):
        record = self._reset(self._record())

        self.assertTrue(record.claim_for_indexing())
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXING)
        self.assertIsNotNone(record.index_started_at)

        self.assertTrue(record.mark_indexed())
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXED)
        self.assertIsNotNone(record.indexed_at)
        self.assertTrue(record.is_searchable)

    def test_pending_to_indexing_to_failed(self):
        record = self._reset(self._record())
        record.claim_for_indexing()

        self.assertTrue(record.mark_index_failed('ConnectionError'))
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.FAILED)
        self.assertEqual(record.index_error, 'ConnectionError')
        self.assertFalse(record.is_searchable)

    def test_pending_to_blocked(self):
        record = self._reset(self._record())

        self.assertTrue(record.mark_index_blocked('not permitted'))
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.BLOCKED)

    def test_failed_can_be_retried(self):
        record = self._reset(self._record(), S.FAILED)

        self.assertTrue(record.claim_for_indexing())
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXING)

    def test_blocked_can_be_retried_once_consent_is_given(self):
        """
        Not in the state diagram, and necessary.

        BLOCKED means the patient had not consented at the time. Leaving those
        records unclaimable would make the block permanent, so granting consent
        would silently never make older records searchable.
        """
        record = self._reset(self._record(), S.BLOCKED)

        self.assertTrue(record.claim_for_indexing())

    def test_each_claim_counts_an_attempt(self):
        record = self._reset(self._record())

        record.claim_for_indexing()
        record.mark_index_failed('boom')
        record.refresh_from_db()
        record.claim_for_indexing()
        record.refresh_from_db()

        self.assertEqual(record.index_attempts, 2)

    def test_a_claim_clears_the_previous_error(self):
        record = self._reset(self._record(), S.FAILED)
        MedicalRecord.objects.filter(pk=record.pk).update(index_error='old')

        record.claim_for_indexing()
        record.refresh_from_db()
        self.assertEqual(record.index_error, '')


class InvalidTransitionTests(_Records):
    """The refusals are the point; each one was previously possible."""

    def test_an_indexed_record_is_not_claimable(self):
        record = self._reset(self._record(), S.INDEXED)

        self.assertFalse(record.claim_for_indexing())
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXED)

    def test_a_record_being_indexed_is_not_claimable(self):
        """ACCEPTANCE — this is the two-workers case."""
        record = self._reset(self._record(), S.INDEXING)

        self.assertFalse(record.claim_for_indexing())

    def test_success_cannot_be_declared_without_holding_the_claim(self):
        """ACCEPTANCE — indexed_at must never be set from PENDING."""
        record = self._reset(self._record())

        self.assertFalse(record.mark_indexed())
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.PENDING)
        self.assertIsNone(record.indexed_at)

    def test_failure_cannot_be_declared_without_holding_the_claim(self):
        record = self._reset(self._record(), S.INDEXED)

        self.assertFalse(record.mark_index_failed('boom'))
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXED)

    def test_a_failure_does_not_erase_a_previous_success(self):
        """
        A record searchable yesterday that failed to re-index today is still
        searchable from the older index. Clearing the timestamp would claim
        otherwise.
        """
        record = self._reset(self._record())
        record.claim_for_indexing()
        record.mark_indexed()
        record.refresh_from_db()
        first_stamp = record.indexed_at

        self._reset(record, S.PENDING)
        record.claim_for_indexing()
        record.mark_index_failed('provider down')
        record.refresh_from_db()

        self.assertEqual(record.index_status, S.FAILED)
        self.assertEqual(record.indexed_at, first_stamp)


class ConcurrencyTests(_Records):
    """Two workers, one record."""

    def test_only_one_of_two_workers_can_claim(self):
        """
        ACCEPTANCE — the compare-and-set, exercised through two separate
        instances of the same row, which is what two workers actually have.
        """
        record = self._reset(self._record())
        worker_a = MedicalRecord.objects.get(pk=record.pk)
        worker_b = MedicalRecord.objects.get(pk=record.pk)

        self.assertTrue(worker_a.claim_for_indexing())
        self.assertFalse(worker_b.claim_for_indexing())

    def test_the_loser_cannot_mark_the_record_indexed(self):
        """The whole failure mode: both workers stamping the same record."""
        record = self._reset(self._record())
        worker_a = MedicalRecord.objects.get(pk=record.pk)
        worker_b = MedicalRecord.objects.get(pk=record.pk)

        worker_a.claim_for_indexing()
        worker_b.claim_for_indexing()          # lost

        self.assertTrue(worker_a.mark_indexed())
        # B thinks it is INDEXING because its in-memory copy is stale; the
        # database disagrees, which is the only opinion that counts.
        self.assertFalse(worker_b.mark_indexed())

        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXED)
        self.assertEqual(record.index_attempts, 1)

    def test_a_stale_worker_cannot_overwrite_a_newer_result(self):
        record = self._reset(self._record())
        stale = MedicalRecord.objects.get(pk=record.pk)
        stale.claim_for_indexing()
        stale.mark_indexed()

        # The record is edited and re-indexed by someone else meanwhile.
        self._reset(record, S.PENDING)
        current = MedicalRecord.objects.get(pk=record.pk)
        current.claim_for_indexing()

        # The old worker finally returns and tries to report failure.
        self.assertFalse(stale.mark_index_failed('late'))
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXING)


class StalenessTests(_Records):

    def test_changed_content_makes_an_indexed_record_claimable_again(self):
        """
        ACCEPTANCE — without this an edited record stays INDEXED, the claim is
        refused, and the new content is never indexed at all.
        """
        record = self._reset(self._record(), S.INDEXED)

        record.title = 'Corrected discharge summary'
        record.save()
        record.refresh_from_db()

        self.assertEqual(record.index_status, S.PENDING)

    def test_a_non_content_save_does_not_disturb_the_state(self):
        record = self._reset(self._record(), S.INDEXED)

        record.is_flagged = True
        record.save(update_fields=['is_flagged'])
        record.refresh_from_db()

        self.assertEqual(record.index_status, S.INDEXED)

    def test_a_record_being_indexed_is_not_reset_mid_flight(self):
        """
        Stealing the state from a running job would let it finish and mark a
        version that no longer exists as current.
        """
        record = self._reset(self._record(), S.INDEXING)

        self.assertFalse(record.mark_index_stale())
        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXING)


class OutcomeFromChunksTests(TestCase):
    """
    The original defect: success was inferred from a COUNT.

    `index_record()` returns how many chunks it created, and chunks exist before
    they are embedded. Refused embeddings and failed embeddings both leave a
    positive count behind, so both were recorded as success — the record shown
    in the patient's list that the assistant then cannot find.

    These run with the synchronous indexer ON, because the pipeline is what is
    being tested.
    """

    def setUp(self):
        self.patient = User.objects.create_user(
            'ox_patient', email='ox@test.invalid', password='pw', role='patient')

    def _record(self):
        return MedicalRecord.objects.create(
            patient=self.patient, title='Discharge summary',
            record_type='discharge', raw_text='Creatinine 180 umol/L')

    def test_refused_embedding_is_blocked_not_indexed(self):
        """ACCEPTANCE — chunks exist, vectors do not, so it is not searchable."""
        from unittest.mock import patch

        from apps.rag_assistant.services.embedding_service import EmbeddingService

        with patch.object(EmbeddingService, 'embed_chunks') as embed:
            def refuse(chunks):
                from apps.rag_assistant.models import MedicalChunk
                for chunk in chunks:
                    chunk.embedding_status = MedicalChunk.EmbeddingStatus.BLOCKED
                    chunk.save(update_fields=['embedding_status'])
            embed.side_effect = refuse
            record = self._record()

        record.refresh_from_db()
        self.assertEqual(record.index_status, S.BLOCKED)
        self.assertIsNone(record.indexed_at)
        self.assertFalse(record.is_searchable)

    def test_failed_embedding_is_failed_not_indexed(self):
        from unittest.mock import patch

        from apps.rag_assistant.services.embedding_service import EmbeddingService

        with patch.object(EmbeddingService, 'embed_chunks') as embed:
            def fail(chunks):
                from apps.rag_assistant.models import MedicalChunk
                for chunk in chunks:
                    chunk.embedding_status = MedicalChunk.EmbeddingStatus.FAILED
                    chunk.save(update_fields=['embedding_status'])
            embed.side_effect = fail
            record = self._record()

        record.refresh_from_db()
        self.assertEqual(record.index_status, S.FAILED)
        self.assertIsNone(record.indexed_at)

    def test_fully_embedded_chunks_are_indexed(self):
        from unittest.mock import patch

        from apps.rag_assistant.services.embedding_service import EmbeddingService

        with patch.object(EmbeddingService, 'embed_chunks') as embed:
            def succeed(chunks):
                from apps.rag_assistant.models import MedicalChunk
                for chunk in chunks:
                    chunk.embedding_status = MedicalChunk.EmbeddingStatus.EMBEDDED
                    chunk.save(update_fields=['embedding_status'])
            embed.side_effect = succeed
            record = self._record()

        record.refresh_from_db()
        self.assertEqual(record.index_status, S.INDEXED)
        self.assertIsNotNone(record.indexed_at)
        self.assertTrue(record.is_searchable)

    def test_a_raising_indexer_leaves_the_record_failed_not_stuck(self):
        """
        A crash must not strand the record at INDEXING, where nothing would ever
        claim it again.
        """
        from unittest.mock import patch

        from apps.rag_assistant.services.rag_service import RAGService

        with patch.object(RAGService, 'index_record',
                          side_effect=RuntimeError('provider down')):
            record = self._record()

        record.refresh_from_db()
        self.assertEqual(record.index_status, S.FAILED)
        self.assertEqual(record.index_error, 'RuntimeError')

    def test_the_error_never_carries_provider_output(self):
        """A provider error can quote the document it was given."""
        from unittest.mock import patch

        from apps.rag_assistant.services.rag_service import RAGService

        with patch.object(RAGService, 'index_record',
                          side_effect=RuntimeError('rejected: Creatinine 180')):
            record = self._record()

        record.refresh_from_db()
        self.assertNotIn('Creatinine', record.index_error)

"""
REGRESSION — N5: medication-status questions could not see clinical notes.

"Am I currently taking metformin?" routes to `medications_node`, which called
`_retrieve(state, 'medication', ...)`. That became a hard SQL filter
`WHERE document__document_type = 'medication'`, so the candidate pool held only
prescription documents. The discontinuation lived in `Clinic Note 2026`
(`record_type='diagnosis'` → `document_type='note'` via the processor's
dtype_map) and was therefore excluded *before ranking*. Measured pool size for
alpha: 1 chunk, discontinuation absent. The assistant answered that the patient
was still taking metformin — confidently, and wrong.

These tests assert on the CANDIDATE POOL, not on the ranked output, because the
defect was exclusion before ranking. The Step-1 reranker guard must never be
what rescues this evidence: a chunk that is not in the pool cannot be ranked at
all. `test_note_is_in_the_pool_before_any_ranking_occurs` pins that distinction.

Embeddings are fabricated locally (np.ones), so the whole file runs offline with
no provider call and no quota.
"""
import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.rag_assistant.models import MedicalChunk, MedicalDocument
from apps.rag_assistant.services.embedding_service import (
    EmbeddingService, active_embedding_dim, active_embedding_model,
)

#: The document types a medication-status question may draw on.
MEDICATION_STATUS_TYPES = ('medication', 'note')

PRESCRIPTION_TEXT = ('Prescription / Medication: Prescription 2025 — 2025-01-10 '
                     'Metformin 1000 mg twice daily with meals. Continue indefinitely.')
DISCONTINUED_TEXT = ('Diagnosis: Clinic Note 2026 — 2026-02-18 Metformin discontinued '
                     'due to persistent gastrointestinal intolerance. Patient advised to stop.')
UNRELATED_NOTE = ('Diagnosis: Nephrology Referral 2026 — 2026-06-01 Referred to '
                  'nephrology for assessment of declining renal function.')


class _Fixture(TestCase):
    """Builds patients whose chunks are embedded without touching a provider."""

    def _user(self, name):
        return get_user_model().objects.create_user(
            username=name, password='pw-test-only', email=f'{name}@example.com')

    def _chunk(self, user, title, document_type, text, record_date):
        doc = MedicalDocument.objects.create(
            patient=user, title=title, document_type=document_type, content=text)
        dim = active_embedding_dim()
        return MedicalChunk.objects.create(
            document=doc, patient=user, chunk_index=0, content=text,
            metadata={'record_date': record_date},
            embedding=np.ones(dim, dtype=np.float32).tobytes(),
            embedding_dimensions=dim,
            embedding_model=active_embedding_model(),
        )

    def pool(self, user, document_type=MEDICATION_STATUS_TYPES):
        """The candidate pool a medication-status query would rank over."""
        texts, _matrix, meta = EmbeddingService().load_patient_embeddings(
            user, document_type)
        return texts, meta


class ContradictionTests(_Fixture):
    """
    THE critical case: an active prescription and a later note that revokes it.
    Both must be available to the answer, with their dates.
    """

    def setUp(self):
        self.user = self._user('med-contradiction')
        self._chunk(self.user, 'Prescription 2025', 'medication',
                    PRESCRIPTION_TEXT, '2025-01-10')
        self._chunk(self.user, 'Clinic Note 2026', 'note',
                    DISCONTINUED_TEXT, '2026-02-18')

    def test_both_pieces_of_evidence_are_available(self):
        """ACCEPTANCE — N5. Was 1 chunk (prescription only) before the fix."""
        texts, _ = self.pool(self.user)
        self.assertEqual(len(texts), 2)
        self.assertTrue(any('Metformin 1000 mg' in t for t in texts))
        self.assertTrue(any('Metformin discontinued' in t for t in texts))

    def test_note_is_in_the_pool_before_any_ranking_occurs(self):
        """
        The fix must not depend on the Step-1 reranker guard rescuing the note.
        This asserts membership at the DB layer, upstream of both ranking stages.
        """
        texts, _ = self.pool(self.user)
        self.assertIn(True, [('Metformin discontinued' in t) for t in texts],
                      'discontinuation note absent from the pre-ranking pool')

    def test_dates_survive_so_the_later_status_can_be_distinguished(self):
        """Without dates the answer cannot tell which statement is current."""
        _texts, meta = self.pool(self.user)
        by_title = {m['document_title']: m for m in meta}
        self.assertEqual(by_title['Prescription 2025']['record_date'], '2025-01-10')
        self.assertEqual(by_title['Clinic Note 2026']['record_date'], '2026-02-18')
        self.assertGreater(by_title['Clinic Note 2026']['record_date'],
                           by_title['Prescription 2025']['record_date'])

    def test_citation_metadata_is_present_for_both(self):
        """Both must be citable, or the answer cannot attribute the conflict."""
        _texts, meta = self.pool(self.user)
        for m in meta:
            self.assertTrue(m['document_id'])
            self.assertTrue(m['document_title'])


class SingleSourceTests(_Fixture):
    """The widened filter must not break the cases that already worked."""

    def test_prescription_only_still_retrievable(self):
        user = self._user('med-prescription-only')
        self._chunk(user, 'Prescription 2025', 'medication',
                    PRESCRIPTION_TEXT, '2025-01-10')
        texts, _ = self.pool(user)
        self.assertEqual(len(texts), 1)
        self.assertIn('Metformin 1000 mg', texts[0])

    def test_note_only_is_retrievable(self):
        """Previously impossible: a note-only medication history was invisible."""
        user = self._user('med-note-only')
        self._chunk(user, 'Clinic Note 2026', 'note',
                    DISCONTINUED_TEXT, '2026-02-18')
        texts, _ = self.pool(user)
        self.assertEqual(len(texts), 1)
        self.assertIn('Metformin discontinued', texts[0])

    def test_unrelated_note_does_not_masquerade_as_medication_evidence(self):
        """
        Widening to notes admits non-medication notes to the pool — that is what
        ranking is for. What must NOT happen is an unrelated note being the only
        thing present and being read as medication status.
        """
        user = self._user('med-unrelated-note')
        self._chunk(user, 'Prescription 2025', 'medication',
                    PRESCRIPTION_TEXT, '2025-01-10')
        self._chunk(user, 'Nephrology Referral 2026', 'note',
                    UNRELATED_NOTE, '2026-06-01')
        texts, _ = self.pool(user)
        self.assertEqual(len(texts), 2)
        medication_texts = [t for t in texts if 'Metformin' in t]
        self.assertEqual(len(medication_texts), 1)
        self.assertNotIn('Metformin', UNRELATED_NOTE)


class IsolationTests(_Fixture):
    """Widening the type filter must not widen the patient boundary."""

    def setUp(self):
        self.mine = self._user('med-mine')
        self.theirs = self._user('med-theirs')
        self._chunk(self.mine, 'Prescription 2025', 'medication',
                    PRESCRIPTION_TEXT, '2025-01-10')
        self._chunk(self.theirs, 'Their Prescription', 'medication',
                    'Prescription: Warfarin 5 mg daily.', '2025-03-01')
        self._chunk(self.theirs, 'Their Clinic Note', 'note',
                    'Diagnosis: Warfarin discontinued after bleeding event.', '2026-01-01')

    def test_another_patients_medication_never_enters_the_pool(self):
        texts, _ = self.pool(self.mine)
        self.assertEqual(len(texts), 1)
        self.assertFalse(any('Warfarin' in t for t in texts))

    def test_another_patients_note_never_enters_the_pool(self):
        """The note type is the newly admitted one — isolation must hold for it."""
        texts, _ = self.pool(self.mine)
        self.assertFalse(any('discontinued after bleeding' in t for t in texts))

    def test_each_patient_sees_only_their_own(self):
        theirs, _ = self.pool(self.theirs)
        self.assertEqual(len(theirs), 2)
        self.assertFalse(any('Metformin' in t for t in theirs))


class FilterSemanticsTests(_Fixture):
    """The widening must be scoped, not global."""

    def setUp(self):
        self.user = self._user('med-filter-semantics')
        self._chunk(self.user, 'Prescription 2025', 'medication',
                    PRESCRIPTION_TEXT, '2025-01-10')
        self._chunk(self.user, 'Clinic Note 2026', 'note',
                    DISCONTINUED_TEXT, '2026-02-18')
        self._chunk(self.user, 'Metabolic Panel 2026', 'lab_result',
                    'Lab result: Glucose 7.8 mmol/L', '2026-05-20')

    def test_lab_route_is_unchanged_by_the_widening(self):
        """A single string filter must still mean exactly that one type."""
        texts, _ = self.pool(self.user, 'lab_result')
        self.assertEqual(len(texts), 1)
        self.assertIn('Glucose', texts[0])

    def test_single_string_filter_still_excludes_notes(self):
        texts, _ = self.pool(self.user, 'medication')
        self.assertEqual(len(texts), 1)
        self.assertIn('Metformin 1000 mg', texts[0])

    def test_medication_status_pool_excludes_lab_results(self):
        """Widening admits notes — not everything."""
        texts, _ = self.pool(self.user)
        self.assertEqual(len(texts), 2)
        self.assertFalse(any('Glucose' in t for t in texts))

    def test_no_filter_returns_everything(self):
        texts, _ = self.pool(self.user, None)
        self.assertEqual(len(texts), 3)

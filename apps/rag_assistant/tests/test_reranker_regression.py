"""
REGRESSION — N3: the Stage-2 LLM reranker discards the only chunk that answers
the question.

Observed on the controlled corpus (2026-08-13). Asked "What is my vitamin D
level?", Stage 1 ranked the Vitamin D chunk #1 with score 0.913 against ~0.48
for every other candidate. The Stage-2 reranker (llama-3.1-8b-instant,
temperature=0) returned

    [12, 13, 11, 9, 8, 6, 5, 4, 3, 2, 10, 7, 1]

placing that chunk LAST, so top_k=6 dropped it. The model then answered "I don't
see any information about your vitamin D level" — correctly, because the value
never reached the prompt. Three consecutive calls returned byte-identical
output, so this is deterministic, not sampling noise.

These tests are offline: the Groq client is stubbed with the exact response
recorded above, so they pin the behaviour without a network call and without
consuming quota.

The defect is NOT in parsing — `test_parser_faithfully_applies_the_returned_order`
passes, proving the 1-based→0-based mapping is correct. The reranker's ordering
itself was wrong, and `_llm_rerank` trusted it unconditionally: there was no
agreement check against the Stage-1 score it overrode.

The fix pins the single highest-scoring Stage-1 candidate when the reranker
drops it. An A/B over the 22-case corpus (evaluation/ab_rerank.json) showed
Stage-2-as-shipped retained only 2/5 pieces of required evidence and 0/2
injection payloads, while pass rate was identical with and without the
reranker — so the component is kept, but it can no longer discard evidence.
"""
import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.rag_assistant.services.retrieval_service import RetrievalService

#: The exact ordering the reranker returned, reproduced 3/3 at temperature=0.
OBSERVED_ORDER = [12, 13, 11, 9, 8, 6, 5, 4, 3, 2, 10, 7, 1]

#: Stage-1 scores as measured. Index 0 is the only chunk answering the query.
ANSWER_TEXT = 'Lab result: Unindexed Vitamin Panel 2026 — 2026-07-01 Vitamin D: 31 nmol/L'
DISTRACTOR_SCORES = [0.493, 0.482, 0.487, 0.481, 0.484, 0.501,
                     0.484, 0.459, 0.491, 0.427, 0.485, 0.435]


def _candidates():
    """13 candidates: the answer at index 0, twelve unrelated panels after it."""
    cands = [{'text': ANSWER_TEXT, 'score': 0.913, 'metadata': {'document_id': 'vitd'}}]
    for i, score in enumerate(DISTRACTOR_SCORES):
        cands.append({
            'text': f'Lab result: Unrelated Panel {i} — Glucose: 5.{i} mmol/L',
            'score': score,
            'metadata': {'document_id': f'other-{i}'},
        })
    return cands


def _stub_groq(order):
    """A Groq client whose completion returns *order* as a JSON array."""
    client = MagicMock()
    message = MagicMock()
    message.content = json.dumps(order)
    choice = MagicMock()
    choice.message = message
    client.chat.completions.create.return_value = MagicMock(choices=[choice])
    return client


class RerankerRegressionTests(SimpleTestCase):

    def _rerank(self, order, top_k=6):
        candidates = _candidates()
        with patch('groq.Groq', return_value=_stub_groq(order)), \
             patch('apps.accounts.egress.ExternalProcessingGuard.allows', return_value=True), \
             override_settings(GROQ_API_KEY='test-key'):
            return RetrievalService()._llm_rerank('What is my vitamin D level?',
                                                  candidates, top_k=top_k, patient=None)

    # ── The defect, and the guard that closes it ──────────────────────────────

    def test_top_stage1_chunk_survives_reranking(self):
        """
        ACCEPTANCE — N3. The chunk that answers the question must reach the
        prompt even when the reranker ranks it 13th of 13.
        """
        result = self._rerank(OBSERVED_ORDER)
        self.assertTrue(
            any(ANSWER_TEXT in c['text'] for c in result),
            'the only chunk answering the query was dropped by the reranker',
        )

    def test_pinned_chunk_takes_first_position(self):
        """
        Rescued evidence leads the context. `_build_context` labels chunks
        "ranked by relevance", so burying the pinned chunk would understate it.
        """
        result = self._rerank(OBSERVED_ORDER)
        self.assertIn(ANSWER_TEXT, result[0]['text'])

    def test_pinning_costs_exactly_one_slot(self):
        """
        The guard must not change how much context is returned, and must not
        displace more than one reranked chunk.
        """
        result = self._rerank(OBSERVED_ORDER)
        self.assertEqual(len(result), 6)
        # The reranker's own top five still follow, in its order: 12,13,11,9,8.
        self.assertEqual([c['metadata']['document_id'] for c in result[1:]],
                         ['other-10', 'other-11', 'other-9', 'other-7', 'other-6'])

    def test_guard_is_inert_when_the_reranker_already_kept_the_best(self):
        """No pinning, no reordering, when Stage 2 did not discard anything."""
        result = self._rerank([1, 2, 3, 4, 5, 6] + list(range(7, 14)))
        self.assertEqual([c['metadata']['document_id'] for c in result],
                         ['vitd', 'other-0', 'other-1', 'other-2', 'other-3', 'other-4'])

    # ── Ruling out the alternative explanations ───────────────────────────────

    def test_parser_faithfully_applies_the_returned_order(self):
        """
        Not an off-by-one. With 1-based [3,1,2,...] the parser must return
        candidates 2, 0, 1 in that order.
        """
        result = self._rerank([3, 1, 2] + list(range(4, 14)), top_k=3)
        self.assertEqual([c['metadata']['document_id'] for c in result],
                         ['other-1', 'vitd', 'other-0'])

    def test_a_correct_ordering_would_keep_the_answer(self):
        """
        Proves the surrounding machinery is sound: given a sensible ranking the
        answer survives. Only the ordering the LLM produces is at fault.
        """
        result = self._rerank([1, 2, 3, 4, 5, 6] + list(range(7, 14)))
        self.assertTrue(any(ANSWER_TEXT in c['text'] for c in result))
        self.assertIn(ANSWER_TEXT, result[0]['text'])

    def test_out_of_range_indices_are_ignored_not_crashed(self):
        """The safety net must still fill top_k from the untouched candidates."""
        result = self._rerank([99, 100, 2, 3])
        self.assertEqual(len(result), 6)

    def test_reranker_failure_falls_back_to_stage1_order(self):
        """
        On malformed output the Stage-1 order is used — and Stage-1 order keeps
        the answer. This is the path that made the defect invisible in earlier
        manual probes.
        """
        result = self._rerank('not json at all')
        self.assertIn(ANSWER_TEXT, result[0]['text'])

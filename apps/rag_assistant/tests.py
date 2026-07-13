"""
Unit tests for the RAG safety and routing layer.

Covers:
  GuardrailService  — 3 safety rules + false-positive resistance
  classify_query_mode — general / hybrid / personal classifier
  _detect_route      — keyword-based document-type router
  RetrievalService._mmr — diversity re-ranking algorithm
"""
import numpy as np
from django.test import SimpleTestCase


# ── GuardrailService ───────────────────────────────────────────────────────────

class GuardrailPreQueryTests(SimpleTestCase):
    """Pre-query emergency gate (fires BEFORE retrieval)."""

    def setUp(self):
        from apps.rag_assistant.services.guardrail_service import GuardrailService
        self.svc = GuardrailService

    def test_chest_pain_triggers_emergency(self):
        triggered, _ = self.svc.check_pre_query("I have severe chest pain")
        self.assertTrue(triggered)

    def test_self_harm_triggers_emergency(self):
        triggered, _ = self.svc.check_pre_query("I want to hurt myself")
        self.assertTrue(triggered)

    def test_suicidal_triggers_emergency(self):
        triggered, _ = self.svc.check_pre_query("I feel suicidal tonight")
        self.assertTrue(triggered)

    def test_normal_query_passes(self):
        triggered, _ = self.svc.check_pre_query("What does my HbA1c result mean?")
        self.assertFalse(triggered)

    def test_partial_word_does_not_trigger(self):
        # "unconsciously" should not match \bunconscious\b
        triggered, _ = self.svc.check_pre_query("I unconsciously eat too much sugar")
        self.assertFalse(triggered)

    def test_emergency_response_contains_phone_number(self):
        _, response = self.svc.check_pre_query("I need an ambulance")
        self.assertIn("911", response)


class GuardrailPostGenerationTests(SimpleTestCase):
    """Post-generation safety rules applied to LLM output."""

    def setUp(self):
        from apps.rag_assistant.services.guardrail_service import GuardrailService
        self.svc = GuardrailService()

    # ── Rule 1: dosage recommendation ─────────────────────────────────────────

    def test_dosage_rule_fires(self):
        text = "You should take 500mg of metformin twice daily."
        safe, rules = self.svc.apply(text)
        self.assertIn('dosage_recommendation', rules)
        self.assertIn('Medication Safety Note', safe)

    def test_dosage_rule_no_false_positive(self):
        # General mention of a dose in an educational context without a directive verb
        text = "Metformin is typically prescribed at 500–2000 mg/day by doctors."
        safe, rules = self.svc.apply(text)
        self.assertNotIn('dosage_recommendation', rules)

    # ── Rule 2: definitive diagnosis ──────────────────────────────────────────

    def test_diagnosis_rule_fires(self):
        text = "Based on these results, you have diabetes."
        safe, rules = self.svc.apply(text)
        self.assertIn('definitive_diagnosis', rules)
        self.assertIn('Diagnostic Note', safe)

    def test_diagnosis_softening_applied(self):
        text = "You have diabetes based on these HbA1c values."
        safe, _ = self.svc.apply(text)
        self.assertIn('may suggest', safe)
        self.assertNotIn('You have diabetes', safe)

    def test_diagnosis_false_positive_benign_have(self):
        # "you have three lab results" must NOT trigger rule 2
        text = "You have three lab results from last month. All look normal."
        safe, rules = self.svc.apply(text)
        self.assertNotIn('definitive_diagnosis', rules)
        # "may suggest" must NOT appear (no softening on benign sentences)
        self.assertNotIn('may suggest', safe)

    def test_diagnosis_false_positive_you_have_many(self):
        text = "You have many options for managing this condition."
        safe, rules = self.svc.apply(text)
        self.assertNotIn('definitive_diagnosis', rules)

    # ── Rule 3: emergency language ────────────────────────────────────────────

    def test_emergency_rule_fires(self):
        text = "This is a medical emergency — seek immediate care."
        safe, rules = self.svc.apply(text)
        self.assertIn('emergency_indicator', rules)
        self.assertIn('Urgent Reminder', safe)

    def test_emergency_no_false_positive(self):
        text = "Your creatinine level is slightly elevated but not alarming."
        safe, rules = self.svc.apply(text)
        self.assertNotIn('emergency_indicator', rules)

    # ── Soft reminder when no rules fire ──────────────────────────────────────

    def test_soft_reminder_added_when_no_rules(self):
        text = "Your cholesterol values are within normal range."
        safe, rules = self.svc.apply(text)
        self.assertEqual(rules, [])
        self.assertIn('consult', safe.lower())

    def test_soft_reminder_not_duplicated_when_already_present(self):
        text = "Your result looks fine. Always consult your healthcare provider."
        safe, rules = self.svc.apply(text)
        self.assertEqual(rules, [])
        count = safe.lower().count('consult')
        self.assertEqual(count, 1)


# ── classify_query_mode ────────────────────────────────────────────────────────

class ClassifyQueryModeTests(SimpleTestCase):

    def setUp(self):
        from apps.rag_assistant.services.general_knowledge_service import classify_query_mode
        self.classify = classify_query_mode

    def test_personal_query(self):
        # "my" = personal marker + "mean" matches a general pattern → hybrid
        self.assertEqual(self.classify("What do my HbA1c results mean?"), 'hybrid')

    def test_general_query_no_personal_marker(self):
        self.assertEqual(self.classify("What is diabetes?"), 'general')

    def test_hybrid_query(self):
        result = self.classify("What is normal creatinine and how does mine compare?")
        self.assertEqual(result, 'hybrid')

    def test_personal_pronoun_without_general_pattern(self):
        self.assertEqual(self.classify("Show me my lab results from last month"), 'personal')

    def test_general_explanation_request(self):
        self.assertEqual(self.classify("Explain what cholesterol is"), 'general')

    def test_hybrid_personal_plus_what_is(self):
        result = self.classify("My TSH is 6.2 — what is the normal range?")
        self.assertEqual(result, 'hybrid')


# ── _detect_route (word-boundary keyword router) ──────────────────────────────

class DetectRouteTests(SimpleTestCase):

    def setUp(self):
        from apps.rag_assistant.graph.nodes import _detect_route
        self.detect = _detect_route

    def test_lab_keyword_routes_to_lab_results(self):
        self.assertEqual(self.detect("What are my cholesterol levels?"), 'lab_results')

    def test_medication_keyword(self):
        # "prescription" and "metformin" are medication keywords; no temporal indicators
        self.assertEqual(self.detect("Show my metformin prescription details"), 'medications')

    def test_wearable_keyword(self):
        self.assertEqual(self.detect("How many steps did I walk last week?"), 'wearable')

    def test_diagnosis_keyword(self):
        self.assertEqual(self.detect("What does my MRI report say?"), 'diagnosis')

    def test_general_fallback(self):
        self.assertEqual(self.detect("Hello, can you help me?"), 'general')

    def test_mg_substring_does_not_route_to_medication(self):
        # "imaging" contains the substring "mg" — word-boundary fix must prevent a
        # false match on the 'mg' medication keyword.  The query also matches
        # 'result' (lab_results) so the multi-route fallback returns 'records', but
        # the important assertion is that 'medications' is NOT returned.
        route = self.detect("What does my imaging result show?")
        self.assertNotEqual(route, 'medications')

    def test_trend_query_routes_to_trajectory(self):
        self.assertEqual(self.detect("Is my HbA1c improving over time?"), 'trajectory')

    def test_journey_phrase_routes_to_trajectory(self):
        self.assertEqual(self.detect("Explain my health journey"), 'trajectory')


# ── RetrievalService._mmr ─────────────────────────────────────────────────────

class MMRTests(SimpleTestCase):
    """MMR re-ranking: selected set should be diverse, not just top-scored."""

    def _make_svc(self):
        """Construct a RetrievalService with default config without hitting the DB."""
        from unittest.mock import patch
        from django.conf import settings

        mock_cfg = {
            'TOP_K': 3,
            'BM25_WEIGHT': 0.35,
            'SEMANTIC_WEIGHT': 0.65,
            'TIME_DECAY_DAYS': 365,
            'TIME_DECAY_FACTOR': 0.15,
            'MMR_LAMBDA': 0.6,
            'SIM_THRESHOLD': 0.15,
            'CONTEXT_TYPE_BOOST': 0.08,
            'INTENT_WEIGHTS': {'general': (0.35, 0.65)},
            'VECTOR_STORE_PATH': 'rag_vector_store/',
        }
        with patch.object(settings, 'RAG_CONFIG', mock_cfg):
            from apps.rag_assistant.services.retrieval_service import RetrievalService
            return RetrievalService()

    def test_mmr_returns_k_indices(self):
        svc    = self._make_svc()
        dim    = 4
        matrix = np.eye(dim, dtype=np.float32)          # perfectly orthogonal vectors
        scores = np.array([0.9, 0.8, 0.7, 0.6], dtype=np.float32)
        valid  = np.arange(dim)
        result = svc._mmr(matrix[0], matrix, scores, valid, k=3)
        self.assertEqual(len(result), 3)

    def test_mmr_selects_diverse_over_similar(self):
        """Given two near-duplicate chunks with high scores and one diverse chunk
        with a lower score, MMR should prefer the diverse chunk over the duplicate."""
        svc = self._make_svc()
        # chunk 0 and chunk 1 are nearly identical; chunk 2 is orthogonal
        v0 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v1 = np.array([0.99, 0.14, 0.0, 0.0], dtype=np.float32)  # near-duplicate of v0
        v2 = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)    # orthogonal

        matrix = np.vstack([v0, v1, v2])
        # v0 highest, v1 second, v2 lowest score
        scores = np.array([0.9, 0.85, 0.6], dtype=np.float32)
        q_vec  = v0.copy()
        valid  = np.arange(3)

        result = svc._mmr(q_vec, matrix, scores, valid, k=2)

        # First pick is always the highest scorer (v0)
        self.assertEqual(result[0], 0)
        # Second pick must be the diverse chunk (v2), not the near-duplicate (v1)
        self.assertEqual(result[1], 2)

    def test_mmr_handles_single_valid_chunk(self):
        svc    = self._make_svc()
        matrix = np.array([[1.0, 0.0]], dtype=np.float32)
        scores = np.array([0.8], dtype=np.float32)
        result = svc._mmr(matrix[0], matrix, scores, np.array([0]), k=3)
        self.assertEqual(result, [0])

    def test_mmr_zero_vector_does_not_crash(self):
        svc = self._make_svc()
        matrix = np.array([
            [1.0, 0.0],
            [0.0, 0.0],  # zero vector
            [0.0, 1.0],
        ], dtype=np.float32)
        scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
        result = svc._mmr(matrix[0], matrix, scores, np.arange(3), k=2)
        self.assertEqual(len(result), 2)

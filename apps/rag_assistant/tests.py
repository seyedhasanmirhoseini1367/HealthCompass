"""
Unit tests for the RAG safety and routing layer.

Covers:
  GuardrailService         — 3 safety rules + false-positive resistance
  classify_query_mode      — general / hybrid / personal classifier
  _detect_route            — keyword-based document-type router
  RetrievalService._mmr    — diversity re-ranking algorithm
  UnitNormalizer           — Finnish SI unit conversions
  QueryUnderstanding       — fast-path classification + biomarker detection
  RetrievalService.rerank  — LLM reranker fallback behaviour
"""
import django.test
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


# ── UnitNormalizer ────────────────────────────────────────────────────────────

class UnitNormalizerTests(SimpleTestCase):
    """Verify SI → conventional unit conversions, especially Finnish lab conventions."""

    def setUp(self):
        from apps.medical_records.unit_normalizer import normalize
        self.normalize = normalize

    # ── Finnish-specific (the whole point of the normalizer) ──────────────────

    def test_hemoglobin_g_per_L_to_g_per_dL(self):
        # FIMLAB reports Hb in g/L (e.g. 145 g/L = 14.5 g/dL)
        val, unit, orig, _ = self.normalize('hemoglobin', '145', 'g/L')
        self.assertAlmostEqual(val, 14.5, places=2)
        self.assertEqual(unit, 'g/dL')
        self.assertEqual(orig, 'g/L')

    def test_hemoglobin_g_per_L_lowercase(self):
        val, unit, _, _ = self.normalize('hgb', '130', 'g/l')
        self.assertAlmostEqual(val, 13.0, places=2)
        self.assertEqual(unit, 'g/dL')

    def test_hba1c_mmol_mol_to_percent(self):
        # 42 mmol/mol is normal HbA1c in Finland (≈ 6.0%)
        # Without conversion it would read as 42% — a lethal misread
        val, unit, orig, _ = self.normalize('hba1c', '42', 'mmol/mol')
        self.assertAlmostEqual(val, 5.99, delta=0.05)
        self.assertEqual(unit, '%')
        self.assertEqual(orig, 'mmol/mol')

    def test_hba1c_diabetic_threshold(self):
        # 53 mmol/mol ≈ 7.0% (diabetic threshold)
        val, unit, _, _ = self.normalize('hba1c', '53', 'mmol/mol')
        self.assertAlmostEqual(val, 7.0, delta=0.1)

    def test_hba1c_alias_hemoglobin_a1c(self):
        val, unit, _, _ = self.normalize('hemoglobin a1c', '48', 'mmol/mol')
        self.assertAlmostEqual(val, 6.54, delta=0.1)
        self.assertEqual(unit, '%')

    # ── Other SI conversions ──────────────────────────────────────────────────

    def test_creatinine_umol_L(self):
        val, unit, _, _ = self.normalize('creatinine', '112', 'umol/L')
        self.assertAlmostEqual(val, 1.267, delta=0.01)
        self.assertEqual(unit, 'mg/dL')

    def test_glucose_mmol_L(self):
        val, unit, _, _ = self.normalize('glucose', '5.5', 'mmol/L')
        self.assertAlmostEqual(val, 99.09, delta=0.5)
        self.assertEqual(unit, 'mg/dL')

    def test_cholesterol_mmol_L(self):
        val, unit, _, _ = self.normalize('total cholesterol', '5.2', 'mmol/L')
        self.assertAlmostEqual(val, 201.1, delta=1.0)
        self.assertEqual(unit, 'mg/dL')

    def test_vitamin_d_nmol_L(self):
        val, unit, _, _ = self.normalize('vitamin d', '50', 'nmol/L')
        self.assertAlmostEqual(val, 20.03, delta=0.1)
        self.assertEqual(unit, 'ng/mL')

    # ── No-conversion pass-through ────────────────────────────────────────────

    def test_no_conversion_needed_mg_dL(self):
        val, unit, orig, _ = self.normalize('creatinine', '1.1', 'mg/dL')
        self.assertAlmostEqual(val, 1.1, places=4)
        self.assertEqual(unit, 'mg/dL')
        self.assertEqual(orig, 'mg/dL')

    def test_unparseable_value_returns_none(self):
        val, unit, _, _ = self.normalize('creatinine', 'N/A', 'µmol/L')
        self.assertIsNone(val)

    def test_comma_decimal_separator(self):
        # European CSV files often use comma as decimal separator
        val, unit, _, _ = self.normalize('creatinine', '112,5', 'umol/L')
        self.assertAlmostEqual(val, 112.5 / 88.4, delta=0.01)


# ── unit_known field ─────────────────────────────────────────────────────────

class UnitKnownTests(SimpleTestCase):
    """
    normalize() must return unit_known=False for known analytes with
    unrecognised units, and True for all safe cases.
    This is the architectural guard that prevents 'dangerous guessing'.
    """

    def setUp(self):
        from apps.medical_records.unit_normalizer import normalize
        self.normalize = normalize

    def test_known_analyte_known_unit_is_true(self):
        # creatinine in µmol/L is in the table → unit_known=True
        _, _, _, known = self.normalize('creatinine', '112', 'µmol/L')
        self.assertTrue(known)

    def test_known_analyte_already_canonical_is_true(self):
        # creatinine in mg/dL is already canonical → unit_known=True
        _, _, _, known = self.normalize('creatinine', '1.1', 'mg/dL')
        self.assertTrue(known)

    def test_known_analyte_unrecognised_unit_is_false(self):
        # creatinine in g/L — not in table, not canonical — dangerous guess
        _, _, _, known = self.normalize('creatinine', '0.001', 'g/L')
        self.assertFalse(known)

    def test_hemoglobin_unrecognised_unit_is_false(self):
        # hemoglobin in oz — nonsensical unit, should be flagged
        _, _, _, known = self.normalize('hemoglobin', '0.5', 'oz')
        self.assertFalse(known)

    def test_hba1c_known_unit_mmol_mol_is_true(self):
        # HbA1c in mmol/mol is in the table → unit_known=True
        _, _, _, known = self.normalize('hba1c', '42', 'mmol/mol')
        self.assertTrue(known)

    def test_hba1c_already_canonical_percent_is_true(self):
        # HbA1c in % is already the canonical unit → unit_known=True
        _, _, _, known = self.normalize('hba1c', '6.0', '%')
        self.assertTrue(known)

    def test_hba1c_unrecognised_unit_is_false(self):
        # HbA1c in some unknown unit — should be flagged
        _, _, _, known = self.normalize('hba1c', '6.0', 'arbitrary')
        self.assertFalse(known)

    def test_unknown_analyte_pass_through_is_true(self):
        # TSH (not in _CONVERSIONS) — pass-through is always safe
        _, _, _, known = self.normalize('tsh', '2.5', 'mIU/L')
        self.assertTrue(known)

    def test_unparseable_value_unit_known_is_true(self):
        # Unparseable value is a different problem from unknown unit
        _, _, _, known = self.normalize('creatinine', 'not-a-number', 'µmol/L')
        self.assertTrue(known)

    def test_unit_known_false_does_not_corrupt_canonical_value(self):
        # When unit_known=False, the raw float is returned (not converted)
        # but callers will exclude it — verify it's still parseable
        val, _, _, known = self.normalize('creatinine', '0.001', 'g/L')
        self.assertFalse(known)
        self.assertIsNotNone(val)   # raw value still returned for display
        self.assertAlmostEqual(val, 0.001, places=4)


# ── QueryUnderstanding ────────────────────────────────────────────────────────

class QueryUnderstandingTests(SimpleTestCase):
    """Fast-path keyword classification and biomarker detection."""

    def setUp(self):
        from apps.rag_assistant.services.query_understanding import understand, _detect_biomarker
        self.understand        = understand
        self.detect_biomarker  = _detect_biomarker

    def test_general_query_no_personal_marker(self):
        qi = self.understand('What is diabetes?')
        self.assertEqual(qi.mode, 'general')
        self.assertFalse(qi.via_llm)

    def test_personal_query(self):
        qi = self.understand('Show me my lab results from last month')
        self.assertEqual(qi.mode, 'personal')

    def test_temporal_route(self):
        qi = self.understand('Is my creatinine improving over time?')
        self.assertTrue(qi.is_temporal)
        self.assertEqual(qi.route, 'trajectory')
        self.assertEqual(qi.biomarker, 'creatinine')

    def test_hybrid_mode(self):
        qi = self.understand('What is normal HbA1c and how does mine compare?')
        self.assertEqual(qi.mode, 'hybrid')

    def test_rewritten_query_unchanged_for_standalone(self):
        qi = self.understand('What is my HbA1c result?')
        self.assertEqual(qi.rewritten_query, 'What is my HbA1c result?')

    # ── Biomarker word-boundary ───────────────────────────────────────────────

    def test_creat_does_not_match_recreate(self):
        # 'creat' alias must not fire on words like 'recreate' or 'created'
        biomarker = self.detect_biomarker('I recreated the meal plan')
        self.assertNotEqual(biomarker, 'creatinine')

    def test_creatinine_full_word_matches(self):
        biomarker = self.detect_biomarker('is my creatinine level high?')
        self.assertEqual(biomarker, 'creatinine')

    def test_hba1c_detected(self):
        qi = self.understand('What does my HbA1c of 42 mmol/mol mean?')
        self.assertEqual(qi.biomarker, 'hba1c')


# ── LLM Reranker fallback ─────────────────────────────────────────────────────

class LLMRerankerFallbackTests(SimpleTestCase):
    """_llm_rerank must degrade gracefully when Groq is unavailable."""

    def _make_svc(self):
        from unittest.mock import patch
        from django.conf import settings
        mock_cfg = {
            'TOP_K': 6, 'BM25_WEIGHT': 0.35, 'SEMANTIC_WEIGHT': 0.65,
            'TIME_DECAY_DAYS': 365, 'TIME_DECAY_FACTOR': 0.15,
            'MMR_LAMBDA': 0.6, 'SIM_THRESHOLD': 0.15,
            'CONTEXT_TYPE_BOOST': 0.08, 'RERANK_RECALL_FACTOR': 5,
            'INTENT_WEIGHTS': {'general': (0.35, 0.65)},
            'VECTOR_STORE_PATH': 'rag_vector_store/',
        }
        with patch.object(settings, 'RAG_CONFIG', mock_cfg):
            from apps.rag_assistant.services.retrieval_service import RetrievalService
            return RetrievalService()

    def _candidates(self, n=5):
        return [{'text': f'chunk {i}', 'score': 1.0 - i * 0.1, 'metadata': {}} for i in range(n)]

    def test_fallback_when_no_groq_key(self):
        from unittest.mock import patch
        svc = self._make_svc()
        with patch('django.conf.settings.GROQ_API_KEY', ''):
            result = svc._llm_rerank('what is my HbA1c?', self._candidates(5), top_k=3)
        self.assertEqual(len(result), 3)
        # Should return first 3 in original order
        self.assertEqual(result[0]['text'], 'chunk 0')

    def test_fallback_when_groq_raises(self):
        from unittest.mock import patch, MagicMock
        svc = self._make_svc()
        with patch('django.conf.settings.GROQ_API_KEY', 'fake-key'):
            with patch('groq.Groq', side_effect=Exception('network error')):
                result = svc._llm_rerank('test query', self._candidates(4), top_k=2)
        self.assertEqual(len(result), 2)

    def test_fallback_when_llm_returns_bad_json(self):
        from unittest.mock import patch, MagicMock
        svc = self._make_svc()
        mock_resp = MagicMock()
        mock_resp.choices[0].message.content = 'not valid json'
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        with patch('django.conf.settings.GROQ_API_KEY', 'fake-key'):
            with patch('groq.Groq', return_value=mock_client):
                result = svc._llm_rerank('test query', self._candidates(3), top_k=2)
        self.assertEqual(len(result), 2)


# ── GuardrailService.get_appended_disclaimers (streaming buffer path) ─────────

class GuardrailAppendedDisclaimersTests(SimpleTestCase):
    """
    get_appended_disclaimers() must return ONLY the appended text (no in-place
    soften) so the streaming buffer caller can yield it without a garbled slice.
    """

    def setUp(self):
        from apps.rag_assistant.services.guardrail_service import GuardrailService
        self.svc = GuardrailService()

    def test_dosage_appends_disclaimer_only(self):
        text = "You should take 500mg of metformin twice daily."
        appended, rules = self.svc.get_appended_disclaimers(text)
        self.assertIn('dosage_recommendation', rules)
        self.assertIn('Medication Safety Note', appended)
        # The original text must NOT be included in the return value
        self.assertNotIn('metformin', appended)

    def test_diagnosis_appends_disclaimer_without_softening_text(self):
        text = "Based on these results, you have diabetes."
        appended, rules = self.svc.get_appended_disclaimers(text)
        self.assertIn('definitive_diagnosis', rules)
        self.assertIn('Diagnostic Note', appended)
        # In-place soften ("may suggest") must NOT appear in the appended text
        self.assertNotIn('may suggest', appended)

    def test_no_rules_returns_soft_reminder(self):
        text = "Your cholesterol is within the normal range."
        appended, rules = self.svc.get_appended_disclaimers(text)
        self.assertEqual(rules, [])
        self.assertIn('consult', appended.lower())

    def test_no_duplicate_reminder_when_already_present(self):
        text = "Looks fine. Always consult your healthcare provider."
        appended, rules = self.svc.get_appended_disclaimers(text)
        self.assertEqual(rules, [])
        self.assertEqual(appended, '')

    def test_length_of_appended_matches_disclaimers_not_full_text(self):
        # Verify the return value doesn't include the original text at all —
        # this is the core invariant that prevents garbled streaming output.
        base = "you have kidney disease based on your creatinine values."
        appended, _ = self.svc.get_appended_disclaimers(base)
        # appended must not contain words from original sentence
        self.assertNotIn('creatinine', appended)
        self.assertNotIn('kidney disease', appended)

    def test_apply_vs_get_appended_consistency(self):
        # Both methods must agree on which rules fired
        text = "You should take 10 units of insulin every morning."
        _, rules_apply    = self.svc.apply(text)
        _, rules_appended = self.svc.get_appended_disclaimers(text)
        self.assertEqual(set(rules_apply), set(rules_appended))


# ── SSE contract integration tests ────────────────────────────────────────────
# Assert that stream_ask() / stream_graph() (single LangGraph pipeline) emit
# the correct SSE event sequence and required JSON keys, and that ask()
# assembles the same events into the expected sync return tuple.
#
# All I/O is mocked at SOURCE modules so tests are fast and fully offline.
# Because stream_graph() uses lazy `from module import X` inside function
# bodies, patching must target the source module, not the importing module:
#   generate_streaming   → apps.rag_assistant.services.generation_service
#   GuardrailService     → apps.rag_assistant.services.guardrail_service
#   TrajectoryService    → apps.rag_assistant.services.trajectory_service
#   health_graph_routing → apps.rag_assistant.graph.graph  (module-level var)

import json
from unittest.mock import MagicMock, patch

_GEN_SVC  = 'apps.rag_assistant.services.generation_service'
_GRD_SVC  = 'apps.rag_assistant.services.guardrail_service'
_TRAJ_SVC = 'apps.rag_assistant.services.trajectory_service'
_GRAPH    = 'apps.rag_assistant.graph.graph'


class SSEContractTests(django.test.SimpleTestCase):
    """
    Integration test: the single LangGraph RAG pipeline must produce the
    correct SSE contract for both streaming (stream_ask / stream_graph) and
    sync (ask) access modes.

    Contract invariants:
        1. Last event is always {"type": "done"}
        2. At least one {"type": "token"} event exists
        3. Event order: token(s)… → sources → meta → done
        4. {"type": "meta"} contains keys: provider, chunks, mode
        5. {"type": "sources"} contains key: sources (list)
        6. No {"type": "error"} event in the normal (non-exception) path
    """

    MOCK_TOKENS  = ['Hello', ', ', 'world', '.']
    # Routing state returned by the mocked health_graph_routing
    MOCK_RSTATE  = {
        'route':              'lab_results',
        'answer':             '',
        'context_chunks':     [],
        'trajectory_context': '',
        'mode':               'personal',
        'rewritten_query':    'What is my creatinine?',
    }

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse(gen):
        """Collect SSE lines from a generator and parse JSON payloads."""
        events = []
        for line in gen:
            if isinstance(line, str) and line.startswith('data: '):
                try:
                    events.append(json.loads(line[6:].strip()))
                except json.JSONDecodeError:
                    pass
        return events

    def _assert_contract(self, events, label=''):
        """Assert all SSE contract invariants hold."""
        types = [e['type'] for e in events]
        ctx   = f' [{label}]' if label else ''

        self.assertEqual(types[-1], 'done', f'Last event must be done{ctx}: {types}')
        self.assertIn('token',   types, f'Must have token event{ctx}')
        self.assertIn('sources', types, f'Must have sources event{ctx}')
        self.assertIn('meta',    types, f'Must have meta event{ctx}')
        self.assertNotIn('error', types, f'Must not have error event{ctx}')

        src_i  = types.index('sources')
        meta_i = types.index('meta')
        done_i = types.index('done')
        last_tok_i = max(i for i, t in enumerate(types) if t == 'token')

        self.assertLess(last_tok_i, src_i,  f'All tokens before sources{ctx}')
        self.assertLess(src_i,      meta_i, f'sources before meta{ctx}')
        self.assertLess(meta_i,     done_i, f'meta before done{ctx}')

        meta = next(e for e in events if e['type'] == 'meta')
        for key in ('provider', 'chunks', 'mode'):
            self.assertIn(key, meta, f'meta missing "{key}"{ctx}')

        sources_ev = next(e for e in events if e['type'] == 'sources')
        self.assertIn('sources', sources_ev)
        self.assertIsInstance(sources_ev['sources'], list)

    def _grd_inst(self):
        """GuardrailService instance mock: no-op apply, no disclaimers."""
        inst = MagicMock()
        inst.apply.side_effect = lambda t: (t, [])
        inst.get_appended_disclaimers.return_value = ('', [])
        return inst

    def _traj_inst(self):
        """TrajectoryService instance mock: not temporal, no chart."""
        inst = MagicMock()
        inst.is_chart_request.return_value = False
        inst.get_trajectory_context.return_value = ('', [])
        return inst

    # ── graph path / streaming ─────────────────────────────────────────────────

    def test_graph_normal_sse_contract(self):
        """stream_graph() emits correct SSE contract for a normal query."""
        grd_inst = self._grd_inst()

        with (
            patch(f'{_GRAPH}.health_graph_routing') as mock_routing,
            patch(f'{_GRD_SVC}.GuardrailService') as grd_cls,
            patch(f'{_GEN_SVC}.generate_streaming',
                  side_effect=lambda *a, **kw: iter(self.MOCK_TOKENS)),
            patch(f'{_GEN_SVC}.active_stream_provider', return_value='groq'),
            patch(f'{_GEN_SVC}._build_sources', return_value=[]),
            patch(f'{_TRAJ_SVC}.TrajectoryService', return_value=self._traj_inst()),
        ):
            grd_cls.check_pre_query.return_value = (False, '')
            grd_cls.return_value = grd_inst
            mock_routing.invoke.return_value = dict(self.MOCK_RSTATE)

            from apps.rag_assistant.graph.graph import stream_graph
            events = self._parse(
                stream_graph(query='What is my creatinine?', patient=MagicMock(pk=1))
            )

        self._assert_contract(events, 'graph-normal')

    def test_graph_emergency_sse_contract(self):
        """stream_graph() emergency route: same structure as legacy emergency."""
        emergency_state = dict(self.MOCK_RSTATE, route='emergency',
                               answer='Call 911 immediately.')

        with (
            patch(f'{_GRAPH}.health_graph_routing') as mock_routing,
            patch(f'{_GRD_SVC}.GuardrailService') as grd_cls,
        ):
            grd_cls.check_pre_query.return_value = (False, '')
            grd_cls.return_value = self._grd_inst()
            mock_routing.invoke.return_value = emergency_state

            from apps.rag_assistant.graph.graph import stream_graph
            events = self._parse(
                stream_graph(query='I have chest pain', patient=MagicMock(pk=1))
            )

        types = [e['type'] for e in events]
        self.assertEqual(types[-1], 'done')
        self.assertIn('token',   types)
        self.assertIn('sources', types)
        self.assertIn('meta',    types)
        meta = next(e for e in events if e['type'] == 'meta')
        self.assertTrue(meta.get('safety_routed'))
        self.assertEqual(meta.get('mode'), 'emergency')

    # ── ask() sync wrapper ─────────────────────────────────────────────────────

    def test_ask_sync_wrapper(self):
        """ask() consumes stream_graph() events and returns the correct 6-tuple."""
        grd_inst = self._grd_inst()

        with (
            patch(f'{_GRAPH}.health_graph_routing') as mock_routing,
            patch(f'{_GRD_SVC}.GuardrailService') as grd_cls,
            patch(f'{_GEN_SVC}.generate_streaming',
                  side_effect=lambda *a, **kw: iter(self.MOCK_TOKENS)),
            patch(f'{_GEN_SVC}.active_stream_provider', return_value='groq'),
            patch(f'{_GEN_SVC}._build_sources', return_value=[{'title': 'Lab result'}]),
            patch(f'{_TRAJ_SVC}.TrajectoryService', return_value=self._traj_inst()),
        ):
            grd_cls.check_pre_query.return_value = (False, '')
            grd_cls.return_value = grd_inst
            mock_routing.invoke.return_value = dict(self.MOCK_RSTATE)

            from apps.rag_assistant.services.rag_service import RAGService
            svc    = RAGService.__new__(RAGService)
            result = svc.ask(MagicMock(pk=1), 'What is my creatinine?')

        response_text, sources, provider, chunks_count, safety_routed, triggered_rules = result
        self.assertEqual(response_text, 'Hello, world.')
        self.assertIsInstance(sources, list)
        self.assertEqual(provider, 'groq')
        self.assertIsInstance(chunks_count, int)
        self.assertFalse(safety_routed)
        self.assertIsInstance(triggered_rules, list)


# ── EmbeddingService dimension check ─────────────────────────────────────────

class EmbeddingDimCheckTests(SimpleTestCase):
    """load_patient_embeddings() must skip stored vectors whose dim != _EMBED_DIM."""

    def test_768_dim_chunk_is_skipped(self):
        import numpy as np
        from unittest.mock import MagicMock, patch
        from apps.rag_assistant.services.embedding_service import EmbeddingService, _EMBED_DIM

        # 768-dim embedding — old text-embedding-004 model
        fake_chunk = MagicMock()
        fake_chunk.embedding = np.zeros(768, dtype=np.float32).tobytes()
        fake_chunk.id = 'legacy-chunk'

        with patch('apps.rag_assistant.models.MedicalChunk') as MockChunk:
            MockChunk.objects.filter.return_value.select_related.return_value = [fake_chunk]

            svc = EmbeddingService.__new__(EmbeddingService)
            svc.cfg        = {'VECTOR_STORE_PATH': 'rag_vector_store/'}
            svc.store_path = 'rag_vector_store/'

            texts, matrix, meta = svc.load_patient_embeddings(MagicMock())

        self.assertEqual(texts, [])
        self.assertEqual(matrix.shape, (0, _EMBED_DIM))
        self.assertEqual(meta, [])

"""
REGRESSION — AI Models P0-1 and P0-2.

P0-2 · Missing input became a confident prediction
--------------------------------------------------
`_build_tabular_df` did:

    val = input_data.get(key, 0)          # missing feature  -> 0
    try:    row[key] = float(...)
    except: row[key] = 0.0                # '' or 'N/A'      -> 0.0

and passed the result straight to the ONNX session. The view builds input_data
with `request.POST.get(key, '')`, so **an entirely empty form produced an
all-zeros feature vector and a confident prediction**. Zero is physiologically
impossible for glucose, BMI, blood pressure and age — but it is a valid-looking
number, so nothing downstream objected, and the fabricated score could cross the
0.75 threshold that raises a HealthAlert.

P0-1 · Demo results presented as clinical findings
---------------------------------------------------
Twelve models are seeded `status='active'` with **no model file**, so every one
runs `_rule_based_demo_result` — an invented weighted sum. They carried names
like "Cardiovascular Risk Score (SCORE2)" and descriptions claiming to be "based
on the ESC SCORE2 framework". A demo score >= 0.75 created a real HealthAlert
titled "High risk detected", and `_static_interpretation` — the path used
whenever external processing is refused — described the fabricated number as
"elevated risk" with no indication it was a placeholder.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.ai_insights.inference.base import InferenceError, InferenceHandler
from apps.ai_insights.inference.interpretation import _static_interpretation
from apps.ai_insights.models import AIModel

SCHEMA = {
    'age':         {'label': 'Age',      'type': 'int',   'min': 18, 'max': 100},
    'glucose':     {'label': 'Glucose',  'type': 'float', 'min': 1,  'max': 40},
    'systolic_bp': {'label': 'Systolic', 'type': 'int',   'min': 60, 'max': 260},
}


class _Handler(InferenceHandler):
    """Minimal concrete handler — only _build_tabular_df is under test."""
    def load_and_preprocess(self, file, filename):     # pragma: no cover
        raise NotImplementedError


class InputRefusalTests(TestCase):
    """P0-2 — refuse, never substitute."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='ai-input', password='pw-test-only', email='ai@example.com')
        self.model = AIModel.objects.create(
            data_scientist=self.user, name='T', description='d',
            input_schema=SCHEMA, status=AIModel.Status.ACTIVE)
        self.handler = _Handler(self.model)

    def test_complete_valid_input_is_accepted(self):
        df = self.handler._build_tabular_df(
            {'age': '55', 'glucose': '6.2', 'systolic_bp': '130'})
        self.assertEqual(list(df.columns), ['age', 'glucose', 'systolic_bp'])
        self.assertEqual(df.iloc[0]['age'], 55.0)

    def test_empty_form_is_refused_not_zero_filled(self):
        """ACCEPTANCE — P0-2. This produced an all-zeros prediction."""
        with self.assertRaises(InferenceError) as ctx:
            self.handler._build_tabular_df(
                {'age': '', 'glucose': '', 'systolic_bp': ''})
        message = str(ctx.exception)
        self.assertIn('missing', message)
        for field in SCHEMA:
            self.assertIn(field, message)

    def test_single_missing_feature_is_refused(self):
        with self.assertRaises(InferenceError) as ctx:
            self.handler._build_tabular_df({'age': '55', 'glucose': '6.2'})
        self.assertIn('systolic_bp', str(ctx.exception))

    def test_absent_key_is_refused(self):
        """`.get(key, 0)` silently invented a value for a key never submitted."""
        with self.assertRaises(InferenceError):
            self.handler._build_tabular_df({'age': '55'})

    def test_non_numeric_is_refused_not_zeroed(self):
        with self.assertRaises(InferenceError) as ctx:
            self.handler._build_tabular_df(
                {'age': 'N/A', 'glucose': '6.2', 'systolic_bp': '130'})
        self.assertIn('not a number', str(ctx.exception))

    def test_nan_and_infinity_are_refused(self):
        for bad in ('nan', 'inf', '-inf'):
            with self.subTest(value=bad):
                with self.assertRaises(InferenceError):
                    self.handler._build_tabular_df(
                        {'age': '55', 'glucose': bad, 'systolic_bp': '130'})

    def test_value_outside_the_declared_range_is_refused(self):
        """
        The bound comes from the model author's own schema, not from any
        clinical judgement made here.
        """
        with self.assertRaises(InferenceError) as ctx:
            self.handler._build_tabular_df(
                {'age': '5', 'glucose': '6.2', 'systolic_bp': '130'})
        self.assertIn('outside the declared range', str(ctx.exception))

    def test_zero_is_still_accepted_when_genuinely_submitted(self):
        """Refusing invented zeros must not reject a real zero."""
        model = AIModel.objects.create(
            data_scientist=self.user, name='T2', description='d',
            input_schema={'smoker': {'label': 'Smoker', 'min': 0, 'max': 1}},
            status=AIModel.Status.ACTIVE)
        df = _Handler(model)._build_tabular_df({'smoker': '0'})
        self.assertEqual(df.iloc[0]['smoker'], 0.0)

    def test_european_decimal_comma_still_parses(self):
        df = self.handler._build_tabular_df(
            {'age': '55', 'glucose': '6,2', 'systolic_bp': '130'})
        self.assertAlmostEqual(df.iloc[0]['glucose'], 6.2)

    def test_all_problems_are_reported_together(self):
        """One refusal listing every fault, not a guessing game field by field."""
        with self.assertRaises(InferenceError) as ctx:
            self.handler._build_tabular_df(
                {'age': '', 'glucose': 'abc', 'systolic_bp': '999'})
        message = str(ctx.exception)
        self.assertIn('missing', message)
        self.assertIn('not a number', message)
        self.assertIn('outside the declared range', message)


class DemoInterpretationTests(TestCase):
    """P0-1 — a fabricated score must never be described as a clinical finding."""

    def test_demo_result_is_labelled_in_the_static_path(self):
        """
        ACCEPTANCE — P0-1. This is the path used when external processing is
        refused, and it previously said nothing about the result being a demo.
        """
        text = _static_interpretation({'risk_score': 0.9, 'label': 'High risk', 'demo': True})
        self.assertIn('DEMONSTRATION RESULT', text)
        self.assertIn('not from a validated medical model', text)

    def test_demo_result_avoids_risk_band_language(self):
        """Calling an invented number 'elevated risk' is the misrepresentation."""
        text = _static_interpretation({'risk_score': 0.9, 'label': 'High risk', 'demo': True})
        self.assertNotIn('elevated risk', text)
        self.assertNotIn('speaking with your doctor soon', text)

    def test_real_result_keeps_its_clinical_wording(self):
        """The demo guard must not degrade genuine model output."""
        text = _static_interpretation({'risk_score': 0.9, 'label': 'High risk', 'demo': False})
        self.assertNotIn('DEMONSTRATION RESULT', text)
        self.assertIn('elevated risk', text)

    def test_demo_result_without_a_score_is_still_labelled(self):
        text = _static_interpretation({'label': 'Completed', 'demo': True})
        self.assertIn('DEMONSTRATION RESULT', text)


class DemoAlertSuppressionTests(TestCase):
    """P0-1 — no HealthAlert from a placeholder."""

    def test_demo_high_score_creates_no_health_alert(self):
        """
        ACCEPTANCE — P0-1. An invented score >= 0.75 raised a real alert titled
        "High risk detected" plus a push notification.
        """
        import pathlib
        source = pathlib.Path(
            'apps/ai_insights/views/catalog.py').read_text(encoding='utf-8-sig')
        self.assertIn("risk_score >= 0.75 and not result.get('demo')", source,
                      'HealthAlert creation must exclude demo results')


class SeededModelHonestyTests(TestCase):
    """
    P0-1 — seeded models must not impersonate published clinical instruments.

    All twelve are seeded active with no model file, so all twelve run the
    rule-based placeholder.
    """

    def setUp(self):
        import pathlib
        self.source = pathlib.Path(
            'apps/ai_insights/management/commands/seed_demo_models.py'
        ).read_text(encoding='utf-8-sig')

    def test_every_seeded_model_is_marked_demo_in_its_name(self):
        import re
        names = re.findall(r"'name':\s+'([^']+)'", self.source)
        self.assertEqual(len(names), 12)
        unmarked = [n for n in names if not n.startswith('[DEMO]')]
        self.assertEqual(unmarked, [],
                         f'seeded models must be visibly demo: {unmarked}')

    def test_no_seeded_model_claims_to_implement_a_published_instrument(self):
        """
        The rule-based fallback is an invented weighted sum. Naming it after
        SCORE2 asserted a validated instrument the code does not implement.
        """
        self.assertNotIn("'[DEMO] Cardiovascular Risk Score (SCORE2)'", self.source)
        self.assertNotIn('Based on the ESC SCORE2 framework', self.source)
        self.assertNotIn('SCORE2 estimates 10-year cardiovascular risk', self.source)

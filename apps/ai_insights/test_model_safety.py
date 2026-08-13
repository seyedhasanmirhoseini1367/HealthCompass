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


class ModelUploadValidationTests(TestCase):
    """
    P1 — model_file had no validation of any kind.

    base.py blocks pickle/Keras/joblib/PyTorch at LOAD time because
    deserialising them executes arbitrary code, but that is after the file is
    already stored. Rejecting at upload keeps it out of storage entirely.
    """

    def setUp(self):
        from apps.ai_insights.forms import SubmitModelForm
        self.form_cls = SubmitModelForm
        self.base = {'name': 'M', 'category': 'general', 'input_type': 'tabular',
                     'description': 'd', 'interpretation_guide': '',
                     'handler_slug': '', 'input_schema_text': '{}',
                     'output_schema_text': '{}', 'handler_config_text': ''}

    def _upload(self, name, content=b'not-a-model'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, content)

    def test_pickle_is_rejected_at_upload(self):
        """ACCEPTANCE — a code-executing format never reaches storage."""
        form = self.form_cls(self.base, {'model_file': self._upload('model.pkl')})
        self.assertFalse(form.is_valid())
        self.assertIn('model_file', form.errors)

    def test_pytorch_is_rejected_at_upload(self):
        form = self.form_cls(self.base, {'model_file': self._upload('model.pt')})
        self.assertFalse(form.is_valid())

    def test_non_onnx_bytes_named_onnx_are_rejected(self):
        """A renamed file must not pass — the check parses, it does not trust the name."""
        form = self.form_cls(self.base, {'model_file': self._upload('model.onnx')})
        self.assertFalse(form.is_valid())
        self.assertIn('model_file', form.errors)

    def test_oversized_model_is_rejected(self):
        from django.test import override_settings
        with override_settings(MAX_MODEL_UPLOAD_BYTES=10):
            form = self.form_cls(
                self.base, {'model_file': self._upload('model.onnx', b'x' * 1000)})
            self.assertFalse(form.is_valid())

    def test_submitting_without_a_model_file_is_still_allowed(self):
        """File-less models are the demo path and must keep working."""
        form = self.form_cls(self.base, {})
        self.assertTrue(form.is_valid(), form.errors)


class SharedInferenceEntryPointTests(TestCase):
    """
    P1 — the mobile API imported apps.ai_insights.runner, which does not exist.

    Every call raised ModuleNotFoundError, so POST /api/v1/ai-models/<slug>/run/
    returned 500 for the whole life of the endpoint. Both callers now share one
    dispatch rather than keeping two divergent copies.
    """

    def test_run_model_is_importable(self):
        from apps.ai_insights.inference import run_model
        self.assertTrue(callable(run_model))

    def test_nothing_imports_the_dead_module(self):
        """
        Parsed with ast, not matched as text: the comments explaining why the
        import was removed necessarily name the module, and a text scan would
        flag the explanation as the offence.
        """
        import ast
        import pathlib

        offenders = []
        for path in pathlib.Path('apps').rglob('*.py'):
            if 'test_' in path.name:
                continue
            try:
                tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            except SyntaxError:                       # pragma: no cover
                self.fail(f'could not parse {path}')
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if 'ai_insights.runner' in node.module:
                        offenders.append(f'{path}:{node.lineno}')
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if 'ai_insights.runner' in alias.name:
                            offenders.append(f'{path}:{node.lineno}')

        self.assertEqual(offenders, [],
                         f'apps.ai_insights.runner does not exist: {offenders}')

    def test_web_view_does_not_duplicate_handler_dispatch(self):
        import pathlib
        source = pathlib.Path(
            'apps/ai_insights/views/catalog.py').read_text(encoding='utf-8-sig')
        self.assertNotIn('_ext_map', source,
                         'handler dispatch belongs in inference.run_model only')


class DebugEndpointTests(TestCase):
    """P1 — debug_handlers was unauthenticated."""

    def test_anonymous_cannot_read_the_handler_dump(self):
        """ACCEPTANCE — it enumerated every model incl. PENDING and REJECTED."""
        from django.urls import reverse
        response = self.client.get(reverse('ai_insights:debug_handlers'))
        self.assertIn(response.status_code, (302, 403))

    def test_ordinary_patient_cannot_read_it(self):
        from django.urls import reverse
        user = get_user_model().objects.create_user(
            username='dbg', password='pw-test-only', email='dbg@example.com')
        self.client.force_login(user)
        self.assertEqual(
            self.client.get(reverse('ai_insights:debug_handlers')).status_code, 403)

    def test_staff_can_read_it(self):
        from django.urls import reverse
        staff = get_user_model().objects.create_user(
            username='dbgstaff', password='pw-test-only', email='s@example.com',
            is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(
            self.client.get(reverse('ai_insights:debug_handlers')).status_code, 200)

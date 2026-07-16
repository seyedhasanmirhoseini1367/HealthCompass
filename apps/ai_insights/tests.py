"""
Security + unit tests for ai_insights inference pipeline.

Key invariants enforced here:
- Only ONNX model files are accepted by _load_model().
- All pickle / Keras / PyTorch full-model formats are hard-blocked.
- A missing or non-existent file raises InferenceError, not an unhandled exception.
- run_prediction returns HTTP 403 for PENDING/REJECTED models regardless of caller.
- APPROVED models are only runnable by their owning data scientist and staff.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.ai_insights.inference.base import (
    InferenceHandler, InferenceError, StandardPrediction, _BLOCKED_FORMATS,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _handler(model_path: str | None, model_file_exists: bool = True) -> InferenceHandler:
    """Build a minimal InferenceHandler pointing at *model_path*."""
    ai_model = MagicMock()
    if model_path is None:
        ai_model.model_file = None
    else:
        ai_model.model_file      = MagicMock()
        ai_model.model_file.path = model_path
    ai_model.handler_config = {}
    return InferenceHandler(ai_model)


# ─── _BLOCKED_FORMATS completeness ────────────────────────────────────────────

class BlockedFormatsSetTest(TestCase):
    """Verify the _BLOCKED_FORMATS constant covers all known dangerous extensions."""

    def test_pickle_variants_present(self):
        for ext in ('pkl', 'pickle'):
            self.assertIn(ext, _BLOCKED_FORMATS, f'.{ext} must be in _BLOCKED_FORMATS')

    def test_keras_variants_present(self):
        for ext in ('h5', 'keras'):
            self.assertIn(ext, _BLOCKED_FORMATS, f'.{ext} must be in _BLOCKED_FORMATS')

    def test_joblib_present(self):
        self.assertIn('joblib', _BLOCKED_FORMATS)

    def test_pytorch_variants_present(self):
        for ext in ('pt', 'pth'):
            self.assertIn(ext, _BLOCKED_FORMATS,
                          f'.{ext} must be blocked — full PyTorch deserialisation executes code')


# ─── _load_model() security gate ──────────────────────────────────────────────

class LoadModelSecurityTest(TestCase):
    """
    _load_model() must raise InferenceError for every blocked format.
    The error message must mention 'blocked' so the caller can show a useful message.
    """

    def _assert_blocked(self, ext: str):
        with patch('os.path.exists', return_value=True):
            h = _handler(f'/srv/models/model.{ext}')
            with self.assertRaises(InferenceError) as ctx:
                h._load_model()
        self.assertIn(
            'blocked', str(ctx.exception).lower(),
            f'.{ext} error message must contain "blocked"',
        )

    def test_pkl_blocked(self):       self._assert_blocked('pkl')
    def test_pickle_blocked(self):    self._assert_blocked('pickle')
    def test_h5_blocked(self):        self._assert_blocked('h5')
    def test_keras_blocked(self):     self._assert_blocked('keras')
    def test_joblib_blocked(self):    self._assert_blocked('joblib')
    def test_pt_blocked(self):        self._assert_blocked('pt')
    def test_pth_blocked(self):       self._assert_blocked('pth')

    def test_unknown_extension_blocked(self):
        """An unrecognised extension (e.g. .bin, .model) must also be rejected."""
        with patch('os.path.exists', return_value=True):
            h = _handler('/srv/models/model.bin')
            with self.assertRaises(InferenceError):
                h._load_model()

    def test_no_extension_blocked(self):
        with patch('os.path.exists', return_value=True):
            h = _handler('/srv/models/modelfile')
            with self.assertRaises(InferenceError):
                h._load_model()


# ─── _load_model() file-not-found / no-file ───────────────────────────────────

class LoadModelFileErrorTest(TestCase):

    def test_no_model_file_raises(self):
        """handler_config without a file must raise InferenceError, not AttributeError."""
        h = _handler(None)
        with self.assertRaises(InferenceError) as ctx:
            h._load_model()
        self.assertIn('no model file', str(ctx.exception).lower())

    def test_missing_file_on_disk_raises(self):
        with patch('os.path.exists', return_value=False):
            h = _handler('/srv/models/gone.onnx')
            with self.assertRaises(InferenceError) as ctx:
                h._load_model()
        self.assertIn('not found', str(ctx.exception).lower())


# ─── _load_model() ONNX happy path ────────────────────────────────────────────

class LoadModelONNXTest(TestCase):

    def test_onnx_attempts_to_load_session(self):
        """
        A valid .onnx path should call onnxruntime.InferenceSession.
        We mock ort so the test passes without a real ONNX binary.
        """
        mock_session = MagicMock()
        mock_ort     = MagicMock()
        mock_ort.InferenceSession.return_value = mock_session

        with patch('os.path.exists', return_value=True), \
             patch.dict('sys.modules', {'onnxruntime': mock_ort}):
            h      = _handler('/srv/models/model.onnx')
            result = h._load_model()

        mock_ort.InferenceSession.assert_called_once_with(
            '/srv/models/model.onnx',
            providers=['CPUExecutionProvider'],
        )
        self.assertIs(result, mock_session)

    def test_onnxruntime_not_installed_gives_clear_error(self):
        # Force ImportError regardless of whether onnxruntime is actually installed.
        # Setting the key to None in sys.modules makes `import onnxruntime` raise
        # ImportError — environment-independent behaviour.
        with patch('os.path.exists', return_value=True), \
             patch.dict('sys.modules', {'onnxruntime': None}):
            h = _handler('/srv/models/model.onnx')
            with self.assertRaises(InferenceError) as ctx:
                h._load_model()
        self.assertIn('onnxruntime', str(ctx.exception).lower())


# ─── StandardPrediction contract ──────────────────────────────────────────────

class StandardPredictionTest(TestCase):
    """StandardPrediction validates and normalises handler output dicts."""

    def test_valid_dict_creates_prediction(self):
        sp = StandardPrediction.from_handler_dict({
            'prediction':       1,
            'prediction_label': 'High Risk',
            'prediction_proba': 0.82,
        })
        self.assertEqual(sp.prediction, 1)
        self.assertEqual(sp.label, 'High Risk')
        self.assertAlmostEqual(sp.risk_score, 0.82)
        self.assertAlmostEqual(sp.confidence, 0.82)

    def test_missing_prediction_raises(self):
        with self.assertRaises(InferenceError) as ctx:
            StandardPrediction.from_handler_dict({'label': 'Low', 'risk_score': 0.2})
        self.assertIn('prediction', str(ctx.exception).lower())

    def test_label_falls_back_to_str_prediction(self):
        sp = StandardPrediction.from_handler_dict({'prediction': 42})
        self.assertEqual(sp.label, '42')

    def test_risk_score_out_of_range_raises(self):
        with self.assertRaises(InferenceError) as ctx:
            StandardPrediction(prediction=1, label='X', risk_score=1.5)
        self.assertIn('risk_score', str(ctx.exception))

    def test_risk_score_none_is_allowed(self):
        sp = StandardPrediction(prediction='pos', label='Positive', risk_score=None)
        self.assertIsNone(sp.risk_score)
        self.assertIsNone(sp.to_result_dict()['risk_score'])

    def test_to_result_dict_has_required_keys(self):
        sp = StandardPrediction(prediction=0, label='Low', risk_score=0.3)
        d = sp.to_result_dict()
        for key in ('success', 'prediction', 'label', 'prediction_label',
                    'risk_score', 'confidence', 'input_summary',
                    'input_data', 'explanation', 'demo'):
            self.assertIn(key, d, f'to_result_dict() must include "{key}"')
        self.assertTrue(d['success'])

    def test_to_result_dict_backward_compat_prediction_label(self):
        """Old templates that read 'prediction_label' must still work."""
        sp = StandardPrediction.from_handler_dict(
            {'prediction': 0, 'prediction_label': 'Negative'}
        )
        d = sp.to_result_dict()
        self.assertEqual(d['prediction_label'], 'Negative')
        self.assertEqual(d['label'], 'Negative')

    def test_demo_flag_propagates(self):
        sp = StandardPrediction.from_handler_dict(
            {'prediction': 0.3, 'label': 'Low risk', 'demo': True}
        )
        self.assertTrue(sp.demo)
        self.assertTrue(sp.to_result_dict()['demo'])

    def test_explanation_defaults_empty(self):
        sp = StandardPrediction.from_handler_dict({'prediction': 1, 'label': 'Yes'})
        self.assertEqual(sp.explanation, '')


# ─── Status enforcement in run_prediction view ─────────────────────────────────

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.ai_insights.models import AIModel


class ModelStatusEnforcementTest(TestCase):
    """
    run_prediction must enforce model.status before calling any handler.

    Invariants:
    - PENDING  → HTTP 403 (not 404; model exists but isn't runnable)
    - REJECTED → HTTP 403
    - APPROVED → HTTP 403 for non-owners; non-403 for the owning data scientist and staff
    - ACTIVE   → non-403 for any authenticated user
    """

    RUN_URL = staticmethod(
        lambda slug: reverse('ai_insights:run_prediction', kwargs={'slug': slug})
    )

    def setUp(self):
        User = get_user_model()
        self.patient   = User.objects.create_user('patient_s',  email='patient_s@test.invalid',  password='pw')
        self.ds_owner  = User.objects.create_user('ds_owner_s', email='ds_owner_s@test.invalid', password='pw')
        self.ds_other  = User.objects.create_user('ds_other_s', email='ds_other_s@test.invalid', password='pw')
        self.staff     = User.objects.create_user('staff_s',    email='staff_s@test.invalid',    password='pw', is_staff=True)

        self.model = AIModel.objects.create(
            name='Status Test Model',
            slug='status-test-model',
            description='Test',
            data_scientist=self.ds_owner,
            input_schema={},
            output_schema={'labels': {'0': 'Low', '1': 'High'}},
            status=AIModel.Status.ACTIVE,
        )

    def _post(self, user, status=None) -> int:
        if status is not None:
            self.model.status = status
            self.model.save(update_fields=['status'])
        self.client.login(username=user.username, password='pw')
        resp = self.client.post(self.RUN_URL(self.model.slug), data={})
        return resp.status_code

    # ── Blocked statuses ──────────────────────────────────────────────────────

    def test_pending_returns_403_for_patient(self):
        self.assertEqual(self._post(self.patient,  AIModel.Status.PENDING), 403)

    def test_pending_returns_403_for_owner(self):
        """Even the model owner cannot run a PENDING model — it needs admin review first."""
        self.assertEqual(self._post(self.ds_owner, AIModel.Status.PENDING), 403)

    def test_rejected_returns_403_for_patient(self):
        self.assertEqual(self._post(self.patient,  AIModel.Status.REJECTED), 403)

    def test_rejected_returns_403_for_owner(self):
        self.assertEqual(self._post(self.ds_owner, AIModel.Status.REJECTED), 403)

    # ── APPROVED: owner and staff only ───────────────────────────────────────

    def test_approved_returns_403_for_patient(self):
        self.assertEqual(self._post(self.patient,  AIModel.Status.APPROVED), 403)

    def test_approved_returns_403_for_other_data_scientist(self):
        self.assertEqual(self._post(self.ds_other, AIModel.Status.APPROVED), 403)

    def test_approved_allows_owner(self):
        """Model owner can test-run their own APPROVED (pre-release) model."""
        self.assertNotEqual(self._post(self.ds_owner, AIModel.Status.APPROVED), 403)

    def test_approved_allows_staff(self):
        """Staff (admin) can run any APPROVED model for review purposes."""
        self.assertNotEqual(self._post(self.staff, AIModel.Status.APPROVED), 403)

    # ── ACTIVE: any authenticated user ──────────────────────────────────────

    def test_active_allows_patient(self):
        """Any logged-in user can run an ACTIVE model."""
        self.assertNotEqual(self._post(self.patient, AIModel.Status.ACTIVE), 403)


# ─── Service layer: get_patient_biomarker_data ────────────────────────────────

import datetime as _dt

from apps.medical_records.models import MedicalRecord, ParsedLabValue


class PatientBiomarkerServiceTest(TestCase):
    """Unit tests for get_patient_biomarker_data — called directly, no HTTP."""

    def setUp(self):
        User = get_user_model()
        self.patient = User.objects.create_user(
            'svc_patient', email='svc_patient@test.invalid', password='pw'
        )
        self.record = MedicalRecord.objects.create(
            patient=self.patient,
            title='Lab 1',
            record_type='lab_result',
            record_date=_dt.date(2024, 1, 15),
        )

    def _add_lab(self, name, value, unit='mg/dL', record=None):
        return ParsedLabValue.objects.create(
            record=record or self.record,
            parameter_name=name,
            value=str(value),
            unit=unit,
        )

    def test_empty_patient_returns_empty_dicts(self):
        from apps.ai_insights.services import get_patient_biomarker_data
        biomarker_map, trending, latest = get_patient_biomarker_data(self.patient)
        self.assertEqual(biomarker_map, {})
        self.assertEqual(trending, {})
        self.assertEqual(latest, {})

    def test_single_data_point_is_in_latest_not_trending(self):
        from apps.ai_insights.services import get_patient_biomarker_data
        self._add_lab('Glucose', 95.0)
        biomarker_map, trending, latest = get_patient_biomarker_data(self.patient)
        self.assertIn('Glucose', biomarker_map)
        self.assertIn('Glucose', latest)
        self.assertNotIn('Glucose', trending)

    def test_two_data_points_appear_in_trending(self):
        from apps.ai_insights.services import get_patient_biomarker_data
        record2 = MedicalRecord.objects.create(
            patient=self.patient, title='Lab 2',
            record_type='lab_result', record_date=_dt.date(2024, 2, 15),
        )
        self._add_lab('Glucose', 95.0)
        self._add_lab('Glucose', 102.0, record=record2)
        biomarker_map, trending, latest = get_patient_biomarker_data(self.patient)
        self.assertIn('Glucose', trending)
        self.assertEqual(len(trending['Glucose']), 2)

    def test_non_numeric_value_is_skipped(self):
        from apps.ai_insights.services import get_patient_biomarker_data
        ParsedLabValue.objects.create(
            record=self.record, parameter_name='Status', value='Positive', unit=''
        )
        biomarker_map, _, _ = get_patient_biomarker_data(self.patient)
        self.assertNotIn('Status', biomarker_map)

    def test_data_point_dict_has_required_keys(self):
        from apps.ai_insights.services import get_patient_biomarker_data
        self._add_lab('HbA1c', 6.2, unit='%')
        biomarker_map, _, _ = get_patient_biomarker_data(self.patient)
        pt = biomarker_map['HbA1c'][0]
        for key in ('date', 'value', 'unit', 'abnormal', 'critical', 'ref'):
            self.assertIn(key, pt, f"Point dict missing key '{key}'")

    def test_only_patient_own_records_returned(self):
        """Service must not leak another patient's lab values."""
        from apps.ai_insights.services import get_patient_biomarker_data
        User = get_user_model()
        other = User.objects.create_user('other_svc', email='other_svc@test.invalid', password='pw')
        other_record = MedicalRecord.objects.create(
            patient=other, title='Other Lab', record_type='lab_result',
            record_date=_dt.date(2024, 3, 1),
        )
        ParsedLabValue.objects.create(
            record=other_record, parameter_name='Sodium', value='140', unit='mEq/L'
        )
        self._add_lab('Glucose', 100)
        biomarker_map, _, _ = get_patient_biomarker_data(self.patient)
        self.assertIn('Glucose', biomarker_map)
        self.assertNotIn('Sodium', biomarker_map)


# ─── Service layer: get_population_risk_buckets ───────────────────────────────

from apps.ai_insights.models import ModelPrediction as _ModelPrediction


class PopulationRiskBucketsServiceTest(TestCase):
    """Unit tests for get_population_risk_buckets — called directly, no HTTP."""

    def setUp(self):
        User = get_user_model()
        self.ds = User.objects.create_user('svc_ds', email='svc_ds@test.invalid', password='pw')
        self.patient = User.objects.create_user(
            'svc_pt2', email='svc_pt2@test.invalid', password='pw'
        )
        self.ai_model = AIModel.objects.create(
            name='Risk Test Model', description='test',
            data_scientist=self.ds,
            input_schema={}, output_schema={},
            status=AIModel.Status.ACTIVE,
        )

    def _add_prediction(self, risk_score):
        return _ModelPrediction.objects.create(
            model=self.ai_model,
            patient=self.patient,
            input_data={},
            result={},
            risk_score=risk_score,
        )

    def test_empty_returns_zero_counts(self):
        from apps.ai_insights.services import get_population_risk_buckets
        buckets, avg_risk, scores = get_population_risk_buckets()
        self.assertEqual(sum(buckets.values()), 0)
        self.assertIsNone(avg_risk)
        self.assertEqual(scores, [])

    def test_low_risk_prediction_counted(self):
        from apps.ai_insights.services import get_population_risk_buckets
        self._add_prediction(0.1)
        buckets, _, _ = get_population_risk_buckets()
        self.assertEqual(buckets['Low (0–30%)'], 1)
        self.assertEqual(buckets['Moderate (30–70%)'], 0)
        self.assertEqual(buckets['High (70–100%)'], 0)

    def test_moderate_risk_prediction_counted(self):
        from apps.ai_insights.services import get_population_risk_buckets
        self._add_prediction(0.5)
        buckets, _, _ = get_population_risk_buckets()
        self.assertEqual(buckets['Moderate (30–70%)'], 1)

    def test_high_risk_prediction_counted(self):
        from apps.ai_insights.services import get_population_risk_buckets
        self._add_prediction(0.9)
        buckets, _, _ = get_population_risk_buckets()
        self.assertEqual(buckets['High (70–100%)'], 1)

    def test_pop_avg_risk_computed_correctly(self):
        from apps.ai_insights.services import get_population_risk_buckets
        self._add_prediction(0.2)
        self._add_prediction(0.8)
        _, avg_risk, _ = get_population_risk_buckets()
        self.assertAlmostEqual(avg_risk, 50.0, places=1)

    def test_none_risk_score_excluded(self):
        from apps.ai_insights.services import get_population_risk_buckets
        _ModelPrediction.objects.create(
            model=self.ai_model, patient=self.patient,
            input_data={}, result={}, risk_score=None,
        )
        _, _, scores = get_population_risk_buckets()
        self.assertEqual(scores, [])


# ─── Service + API consistency test ──────────────────────────────────────────

from rest_framework.test import APIClient as _APIClient


class BiomarkerServiceAPIConsistencyTest(TestCase):
    """
    Calls get_patient_biomarker_data directly AND the /api/v1/analytics/ endpoint,
    then asserts they agree on the same biomarker names and data-point count.

    The API uses JWT authentication; we bypass token issuance via
    APIClient.force_authenticate so the test stays focused on data consistency.
    """

    def setUp(self):
        User = get_user_model()
        self.patient = User.objects.create_user(
            'consist_pt', email='consist_pt@test.invalid', password='pw'
        )
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Consistency Lab',
            record_type='lab_result', record_date=_dt.date(2024, 5, 1),
        )
        for name, val in [('Glucose', 95.0), ('HbA1c', 6.1), ('Cholesterol', 180.0)]:
            ParsedLabValue.objects.create(
                record=record, parameter_name=name, value=str(val), unit='mg/dL'
            )
        self.api_client = _APIClient()
        self.api_client.force_authenticate(user=self.patient)

    def test_api_biomarker_latest_matches_service(self):
        """API response biomarker_latest keys == service biomarker_map keys."""
        from apps.ai_insights.services import get_patient_biomarker_data

        # 1. Direct service call
        biomarker_map, _, _ = get_patient_biomarker_data(self.patient)

        # 2. API call (force_authenticate bypasses JWT so this is an integration test)
        resp = self.api_client.get('/api/v1/analytics/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        api_biomarker_keys = set(data['biomarker_latest'].keys())
        svc_biomarker_keys = set(biomarker_map.keys())
        self.assertEqual(api_biomarker_keys, svc_biomarker_keys)

    def test_api_total_biomarkers_matches_service(self):
        """API response total_biomarkers count == len(service biomarker_map)."""
        from apps.ai_insights.services import get_patient_biomarker_data

        biomarker_map, _, _ = get_patient_biomarker_data(self.patient)

        resp = self.api_client.get('/api/v1/analytics/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data['total_biomarkers'], len(biomarker_map))

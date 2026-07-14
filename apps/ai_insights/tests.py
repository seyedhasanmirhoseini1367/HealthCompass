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

from apps.ai_insights.inference.base import InferenceHandler, InferenceError, _BLOCKED_FORMATS


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
        import sys
        saved = sys.modules.pop('onnxruntime', None)
        try:
            with patch('os.path.exists', return_value=True):
                h = _handler('/srv/models/model.onnx')
                with self.assertRaises(InferenceError) as ctx:
                    h._load_model()
            self.assertIn('onnxruntime', str(ctx.exception).lower())
        finally:
            if saved is not None:
                sys.modules['onnxruntime'] = saved


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

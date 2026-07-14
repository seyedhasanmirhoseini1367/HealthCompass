"""
Security + unit tests for ai_insights inference pipeline.

Key invariants enforced here:
- Only ONNX model files are accepted by _load_model().
- All pickle / Keras / PyTorch full-model formats are hard-blocked.
- A missing or non-existent file raises InferenceError, not an unhandled exception.
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

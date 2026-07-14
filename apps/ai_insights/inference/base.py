# ai_insights/inference/base.py
"""
Base class for every HealthCompass inference handler.

Contract
--------
Handler receives:
    - self.ai_model  : the AIModel instance
    - self.cfg       : ai_model.handler_config dict (set in Django admin)

Handler must implement:
    validate_file(file, filename)       → None  (raise InferenceError on bad input)
    load_and_preprocess(file, filename) → (feature_df, input_summary_dict)
    postprocess(raw_pred, proba, feat)  → dict

The run() method orchestrates all steps + model loading.
For PyTorch models (EEG handlers), override run() entirely and load weights
with torch.load(..., weights_only=True) + model.load_state_dict().
"""

import os
import io
from dataclasses import dataclass, field as _dc_field
import numpy as np
import pandas as pd

# Formats that allow arbitrary code execution when deserialised.
# _load_model() hard-blocks every one of these — only ONNX is accepted.
_BLOCKED_FORMATS = frozenset({'pkl', 'pickle', 'h5', 'keras', 'joblib', 'pt', 'pth'})


class InferenceError(ValueError):
    """Raised for user-caused errors (wrong format, missing columns, bad file).
    Message is shown directly to the patient in the UI."""


@dataclass
class StandardPrediction:
    """
    Validated output contract for every HealthCompass inference handler.

    Required
    --------
    prediction   : raw model output — class index, float score, string, etc.
    label        : human-readable result shown to the patient

    Optional
    --------
    risk_score   : float in [0, 1]; required for binary/risk-classification models
    confidence   : probability of the predicted class (often equals risk_score)
    input_summary: dict or string describing the parsed input shown in the UI
    input_data   : key→value pairs used for inference (shown in history)
    explanation  : optional static per-handler plain-text note (≠ AI interpretation)
    demo         : True when the result comes from rule-based logic, not a real model
    """

    prediction:    object
    label:         str
    risk_score:    'float | None'    = None
    confidence:    'float | None'    = None
    input_summary: object            = None
    input_data:    dict              = _dc_field(default_factory=dict)
    explanation:   str               = ''
    demo:          bool              = False

    def __post_init__(self):
        self.label = str(self.label)
        if self.risk_score is not None and not (0.0 <= float(self.risk_score) <= 1.0):
            raise InferenceError(
                f'risk_score must be in [0.0, 1.0], got {self.risk_score!r}. '
                "Check your handler's postprocess() method."
            )

    def to_result_dict(self) -> dict:
        """Return the dict that catalog.py, templates, and ModelPrediction consume."""
        return {
            'success':          True,
            'prediction':       self.prediction,
            'prediction_label': self.label,   # kept for backward-compat with old templates
            'label':            self.label,
            'risk_score':       self.risk_score,
            'confidence':       self.confidence,
            'input_summary':    self.input_summary if self.input_summary is not None else {},
            'input_data':       self.input_data,
            'explanation':      self.explanation,
            'demo':             self.demo,
        }

    @classmethod
    def from_handler_dict(cls, d: dict) -> 'StandardPrediction':
        """Validate and normalise a raw handler-output dict into StandardPrediction."""
        prediction = d.get('prediction')
        if prediction is None:
            raise InferenceError(
                'Handler postprocess() must include "prediction" in its return value. '
                f'Keys returned: {sorted(d)}. '
                'Add "prediction" (the raw model output) to the dict.'
            )
        label = d.get('label') or d.get('prediction_label') or str(prediction)

        # risk_score: explicit field wins; fall back to raw probability
        risk_score = d.get('risk_score')
        if risk_score is None:
            risk_score = d.get('prediction_proba')

        # confidence: the model probability (may equal risk_score for binary classifiers)
        confidence = d.get('prediction_proba')
        if confidence is None:
            confidence = d.get('confidence') or risk_score

        return cls(
            prediction    = prediction,
            label         = str(label),
            risk_score    = float(risk_score)    if risk_score    is not None else None,
            confidence    = float(confidence)    if confidence    is not None else None,
            input_summary = d.get('input_summary'),
            input_data    = d.get('input_data') or {},
            explanation   = d.get('explanation', ''),
            demo          = d.get('demo', False),
        )


class InferenceHandler:
    """Abstract base for all model-specific inference handlers."""

    # Subclasses declare accepted extensions — used for UI hints + validate_file().
    accepted_extensions: list[str] = []

    def __init__(self, ai_model):
        self.ai_model = ai_model
        self.cfg      = ai_model.handler_config or {}

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self, uploaded_file=None, input_data: dict | None = None) -> dict:
        """
        Full inference pipeline for tabular/CSV ONNX models.
        Returns a dict:
        {
            "success":           True,
            "prediction":        <raw value>,
            "prediction_label":  <human label>,
            "risk_score":        <float 0-1 | None>,
            "label":             <same as prediction_label>,
            "input_summary":     { ... },
            "input_data":        { ... },
        }
        """
        if uploaded_file is not None:
            filename = getattr(uploaded_file, 'name', '').lower()
            self.validate_file(uploaded_file, filename)
            feature_df, input_summary = self.load_and_preprocess(uploaded_file, filename)
            self._validate_features(feature_df)
        else:
            feature_df    = self._build_tabular_df(input_data or {})
            input_summary = {'source': 'manual form', 'fields': len(input_data or {})}

        sess       = self._load_model()    # always InferenceSession after security fix
        input_name = sess.get_inputs()[0].name
        X          = feature_df.values.astype(np.float32)
        outputs    = sess.run(None, {input_name: X})

        raw_pred = outputs[0]
        proba    = None
        if len(outputs) > 1 and isinstance(outputs[1], list) and outputs[1]:
            prob_map = outputs[1][0]
            if isinstance(prob_map, dict):
                proba = float(max(prob_map.values()))
            elif hasattr(prob_map, 'max'):
                proba = float(prob_map.max())

        result = self.postprocess(raw_pred, proba, feature_df)
        result['input_summary'] = input_summary
        result['input_data'] = {
            k: (round(v, 6) if isinstance(v, float) else v)
            for k, v in dict(zip(feature_df.columns, feature_df.iloc[0].tolist())).items()
        }
        return StandardPrediction.from_handler_dict(result).to_result_dict()

    # ── Subclass interface ────────────────────────────────────────────────────

    def validate_file(self, file, filename: str) -> None:
        if self.accepted_extensions:
            ext = filename.rsplit('.', 1)[-1] if '.' in filename else ''
            if ext not in self.accepted_extensions:
                raise InferenceError(
                    f'Expected: {", ".join(self.accepted_extensions).upper()}. '
                    f'You uploaded: .{ext.upper()}.'
                )

    def load_and_preprocess(self, file, filename: str):
        raise NotImplementedError('Subclass must implement load_and_preprocess()')

    def postprocess(self, raw_prediction, proba: float | None, feature_df: pd.DataFrame) -> dict:
        label_map = self.cfg.get('label_map', {})
        pred_val  = raw_prediction[0]
        if hasattr(pred_val, 'item'):
            pred_val = pred_val.item()
        key   = str(int(pred_val)) if isinstance(pred_val, float) and pred_val == int(pred_val) else str(pred_val)
        label = label_map.get(key, str(pred_val))
        return {
            'prediction':       pred_val,
            'prediction_label': label,
            'prediction_proba': proba,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_model(self):
        """
        Load the model file as an onnxruntime InferenceSession.

        Only .onnx files are accepted.  All other formats (pickle, Keras, PyTorch
        full-model serialisation) are hard-blocked because deserialising them
        executes arbitrary Python code — equivalent to running any uploaded file
        as root.  PyTorch EEG handlers are safe because they construct the
        architecture in Python and only load state_dict (weights_only=True).
        """
        if not self.ai_model.model_file:
            raise InferenceError(
                'No model file uploaded. '
                'Upload a .onnx file via the Django admin panel.'
            )
        path = self.ai_model.model_file.path
        if not os.path.exists(path):
            raise InferenceError(
                f'Model file not found on disk: {os.path.basename(path)}. '
                'Please re-upload the model file via Django admin.'
            )

        ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''

        if ext in _BLOCKED_FORMATS:
            raise InferenceError(
                f'.{ext} models are blocked for security reasons — '
                'pickle, Keras, and unsandboxed PyTorch serialisation execute '
                'arbitrary code when loaded. '
                'Convert to ONNX and re-upload (use the bundled convert_to_onnx.py).'
            )

        if ext == 'onnx':
            try:
                import onnxruntime as ort
            except ImportError:
                raise InferenceError(
                    'onnxruntime is not installed. Run: pip install onnxruntime'
                )
            return ort.InferenceSession(path, providers=['CPUExecutionProvider'])

        raise InferenceError(
            f'Unsupported model format: .{ext}. '
            'Only .onnx is accepted.'
        )

    def _validate_features(self, feature_df: pd.DataFrame) -> None:
        expected_n = self.cfg.get('expected_n_features')
        if expected_n is None:
            return
        actual_n = feature_df.shape[1]
        if actual_n != expected_n:
            raise InferenceError(
                f'Feature count mismatch: model trained on {expected_n} features, '
                f'but your file produced {actual_n}. '
                'Check that your file has the correct columns/channels.'
            )

    def _build_tabular_df(self, input_data: dict) -> pd.DataFrame:
        """Convert form POST data to a one-row DataFrame in schema order."""
        schema = self.ai_model.input_schema or {}
        row = {}
        for key in (schema.keys() if schema else input_data.keys()):
            val = input_data.get(key, 0)
            try:
                row[key] = float(str(val).replace(',', '.'))
            except (TypeError, ValueError):
                row[key] = 0.0
        return pd.DataFrame([row])

    # ── Shared parsing utilities ──────────────────────────────────────────────

    @staticmethod
    def read_csv(file) -> pd.DataFrame:
        try:
            return pd.read_csv(file)
        except Exception as e:
            raise InferenceError(f'Could not read CSV: {e}')

    @staticmethod
    def read_parquet(file) -> pd.DataFrame:
        data = io.BytesIO(file.read()) if not isinstance(file, (bytes, io.BytesIO)) else file
        try:
            return pd.read_parquet(data)
        except ImportError:
            raise InferenceError('Parquet support requires pyarrow. Run: pip install pyarrow')
        except Exception as parquet_err:
            # Fall back to CSV if parquet engine unavailable or wrong format
            try:
                if hasattr(data, 'seek'):
                    data.seek(0)
                return pd.read_csv(data)
            except Exception:
                raise InferenceError(f'Could not read file as Parquet or CSV: {parquet_err}')

    @staticmethod
    def read_image(file, target_size: tuple | None = None) -> np.ndarray:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(file.read())).convert('RGB')
            if target_size:
                img = img.resize(target_size)
            return np.array(img)
        except ImportError:
            raise InferenceError('Image support requires Pillow. Run: pip install Pillow')
        except Exception as e:
            raise InferenceError(f'Could not read image: {e}')

    @staticmethod
    def read_edf(file) -> tuple:
        import tempfile, shutil, os as _os
        with tempfile.NamedTemporaryFile(suffix='.edf', delete=False) as tmp:
            shutil.copyfileobj(file, tmp)
            tmp_path = tmp.name
        try:
            try:
                import mne
                raw = mne.io.read_raw_edf(tmp_path, preload=True, verbose=False)
                data, _ = raw.get_data(return_times=True)
                df = pd.DataFrame(data.T, columns=raw.ch_names)
                summary = {
                    'format': 'EDF', 'channels': raw.ch_names,
                    'n_channels': len(raw.ch_names), 'n_samples': data.shape[1],
                    'sampling_rate_hz': raw.info['sfreq'],
                }
                return df, summary
            except ImportError:
                pass
            raise InferenceError(
                'EDF reading requires mne. Run: pip install mne'
            )
        finally:
            _os.unlink(tmp_path)

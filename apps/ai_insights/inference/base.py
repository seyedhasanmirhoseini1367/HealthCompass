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
For non-sklearn/keras models (e.g. PyTorch), override run() entirely (see seizure_eeg.py).
"""

import os
import io
import pickle
import numpy as np
import pandas as pd


class InferenceError(ValueError):
    """Raised for user-caused errors (wrong format, missing columns, bad file).
    Message is shown directly to the patient in the UI."""


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
        Full inference pipeline.
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
            # Tabular input from form
            feature_df    = self._build_tabular_df(input_data or {})
            input_summary = {'source': 'manual form', 'fields': len(input_data or {})}

        model   = self._load_model()
        raw_pred = model.predict(feature_df)

        proba = None
        if hasattr(model, 'predict_proba'):
            try:
                proba = float(model.predict_proba(feature_df)[0].max())
            except Exception:
                pass

        result = self.postprocess(raw_pred, proba, feature_df)
        result['input_summary'] = input_summary
        result['input_data'] = {
            k: (round(v, 6) if isinstance(v, float) else v)
            for k, v in dict(zip(feature_df.columns, feature_df.iloc[0].tolist())).items()
        }
        result['success']    = True
        result['risk_score'] = result.get('prediction_proba') or result.get('risk_score')
        result['label']      = result.get('prediction_label', str(result.get('prediction', '')))
        return result

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
        if not self.ai_model.model_file:
            raise InferenceError(
                'No model file uploaded for this model. '
                'The data scientist must upload a .pkl, .h5, or .pt file via the admin panel.'
            )
        path = self.ai_model.model_file.path
        if not os.path.exists(path):
            raise InferenceError(
                f'Model file not found on disk: {os.path.basename(path)}. '
                'Please re-upload the model file via Django admin.'
            )
        try:
            if path.endswith(('.pkl', '.pickle')):
                with open(path, 'rb') as f:
                    return pickle.load(f)
            if path.endswith('.joblib'):
                import joblib
                return joblib.load(path)
            if path.endswith(('.h5', '.keras')):
                from tensorflow import keras
                return keras.models.load_model(path)
            if path.endswith(('.pt', '.pth')):
                import torch
                return torch.load(path, map_location='cpu', weights_only=False)
            raise InferenceError(
                f'Unsupported model format: {os.path.basename(path)}. '
                'Supported: .pkl, .joblib, .h5, .keras, .pt, .pth'
            )
        except InferenceError:
            raise
        except Exception as e:
            raise InferenceError(f'Failed to load model: {e}')

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

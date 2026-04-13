# ai_insights/inference/eeg_csv.py
"""
EEG / physiological signal handler for HealthCompass.
Ported from PersonalPortfolio seizure_eeg.py — identical pipeline.

Pipeline
--------
1.  Read Parquet/CSV  →  drop EKG column if present
2.  Centre-crop 10 seconds  (centre ± 1000 samples at 200 Hz)
3.  Bandpass filter each channel  (0.5–30 Hz, Butterworth order 4)
4.  StandardScaler normalisation
5.  STFT spectrogram per channel  (nperseg=128, noverlap=64)
6.  Stack into tensor  (1, n_channels, freq_bins, time_steps)
7.  Build model architecture  →  load state_dict  →  forward  →  softmax

handler_slug: "eeg_csv"

handler_config:
{
    "model_variant":     "cnn_simple",      // or "cnn_transformer"
    "sampling_rate_hz":  200,
    "n_channels":        19,
    "nperseg":           128,
    "label_map":         {"0": "LPD", "1": "Seizure"}
}
"""

import io
import os
import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt, stft as scipy_stft
from sklearn.preprocessing import StandardScaler

from .registry import register
from .base import InferenceHandler, InferenceError


# ─── Signal processing (matches training code exactly) ────────────────────────

def _bandpass_filter(data: np.ndarray, lowcut=0.5, highcut=30.0,
                     fs=200, order=4) -> np.ndarray:
    nyq  = 0.5 * fs
    b, a = butter(order, [lowcut / nyq, highcut / nyq], btype='band')
    return filtfilt(b, a, data)


def _create_stft_spectrogram(signal: np.ndarray, fs: int = 200,
                              nperseg: int = 128, noverlap: int = None) -> np.ndarray:
    if noverlap is None:
        noverlap = nperseg // 2
    if len(signal) < nperseg:
        raise InferenceError(
            f'Signal segment too short for STFT: {len(signal)} samples, '
            f'need at least {nperseg}.'
        )
    _, _, spec = scipy_stft(signal, fs=fs, nperseg=nperseg, noverlap=noverlap)
    spec = np.abs(spec)
    mn, mx = spec.min(), spec.max()
    if mx > mn:
        spec = (spec - mn) / (mx - mn)
    return np.log1p(spec)   # (freq_bins, time_steps)


def _preprocess_eeg(df: pd.DataFrame, fs: int = 200,
                    nperseg: int = 128, n_channels: int = 19) -> np.ndarray:
    """Returns tensor (1, n_channels, freq_bins, time_steps) as float32."""
    total   = len(df)
    centre  = total // 2
    start   = max(centre - 1000, 0)
    end     = min(centre + 1000, total)

    if (end - start) < nperseg:
        raise InferenceError(
            f'File has only {total} samples — too short. '
            f'Need at least {nperseg} samples (≈ {nperseg/fs:.1f} s at {fs} Hz). '
            f'Upload ≥ 10 seconds of EEG ({10*fs} samples).'
        )

    segment  = df.iloc[start:end, :n_channels].copy()
    filtered = pd.DataFrame(
        {col: _bandpass_filter(segment[col].values, fs=fs)
         for col in segment.columns},
        index=segment.index,
    )
    scaled = pd.DataFrame(
        StandardScaler().fit_transform(filtered),
        columns=filtered.columns,
    )

    spectrograms = []
    for i in range(min(n_channels, scaled.shape[1])):
        spec = _create_stft_spectrogram(scaled.iloc[:, i].values,
                                        fs=fs, nperseg=nperseg)
        spectrograms.append(spec)

    if len(spectrograms) < n_channels:
        raise InferenceError(
            f'File has {scaled.shape[1]} EEG channels, model needs {n_channels}.'
        )

    return np.stack(spectrograms, axis=0)[np.newaxis, ...].astype(np.float32)


# ─── Model architectures ──────────────────────────────────────────────────────

def _build_cnn_simple(num_classes: int = 2):
    import torch
    import torch.nn as nn

    class CNNSimple(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Sequential(
                nn.Conv2d(1, 64, kernel_size=7, padding=3),
                nn.BatchNorm2d(64), nn.GELU(), nn.MaxPool2d(2, 2),
            )
            self.conv2 = nn.Sequential(
                nn.Conv2d(64, 128, kernel_size=5, padding=2),
                nn.BatchNorm2d(128), nn.GELU(), nn.MaxPool2d(2, 2),
            )
            self.global_pool    = nn.AdaptiveAvgPool2d((1, 1))
            self.self_attention = nn.MultiheadAttention(embed_dim=128,
                                                        num_heads=4,
                                                        batch_first=True)
            self.layer_norm = nn.LayerNorm(128)
            self.fc1        = nn.Linear(128, 64)
            self.fc2        = nn.Linear(64, num_classes)
            self.dropout    = nn.Dropout(0.3)

        def forward(self, x):
            batch, n_ch, freq, time = x.shape
            ch_features = []
            for c in range(n_ch):
                ch = x[:, c:c+1, :, :]
                ch = self.conv1(ch)
                ch = self.conv2(ch)
                ch = self.global_pool(ch).squeeze(-1).squeeze(-1)
                ch_features.append(ch)
            seq = torch.stack(ch_features, dim=1)
            attn_out, _ = self.self_attention(seq, seq, seq)
            out = self.layer_norm(attn_out.mean(dim=1))
            return self.fc2(self.dropout(torch.relu(self.fc1(out))))

    return CNNSimple()


def _build_cnn_transformer(num_classes: int = 2):
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class PositionalEncoding(nn.Module):
        def __init__(self, d_model, max_len=5000):
            super().__init__()
            if d_model % 2 != 0:
                d_model += 1
            pe  = torch.zeros(max_len, d_model)
            pos = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
            div = torch.exp(torch.arange(0, d_model, 2).float()
                            * -(np.log(10000.0) / d_model))
            pe[:, 0::2] = torch.sin(pos * div)
            pe[:, 1::2] = torch.cos(pos * div)
            self.register_buffer('pe', pe.unsqueeze(0))

        def forward(self, x):
            return x + self.pe[:, :x.size(1), :x.size(2)]

    class CNNTransformerClassifier(nn.Module):
        def __init__(self, num_classes=2, num_eeg_channels=19,
                     cnn_out=(32, 16, 8), d_model=100,
                     num_heads=4, n_layers=2, dropout=0.5):
            super().__init__()
            self.num_eeg_channels = num_eeg_channels

            def cb(ic, oc, k, p):
                return nn.Sequential(
                    nn.Conv2d(ic, oc, k, padding=p),
                    nn.BatchNorm2d(oc), nn.GELU(), nn.MaxPool2d(2, 2),
                )

            self.conv1 = cb(1, cnn_out[0], (5, 5), (1, 1))
            self.conv2 = cb(cnn_out[0], cnn_out[1], (3, 3), (2, 2))
            self.conv3 = nn.Sequential(
                nn.Conv2d(cnn_out[1], cnn_out[2], (3, 3), padding=(3, 3)),
                nn.BatchNorm2d(cnn_out[2]), nn.GELU(),
            )
            self.sa1   = nn.Sequential(nn.Conv2d(cnn_out[0], 1, 1), nn.Sigmoid())
            self.sa2   = nn.Sequential(nn.Conv2d(cnn_out[1], 1, 1), nn.Sigmoid())
            self.sa3   = nn.Sequential(nn.Conv2d(cnn_out[2], 1, 1), nn.Sigmoid())
            self.pool  = nn.AdaptiveAvgPool2d((1, 1))
            self.feature_fusion = nn.Linear(sum(cnn_out), d_model)
            self.pos_encoder    = PositionalEncoding(d_model)
            enc = nn.TransformerEncoderLayer(d_model, num_heads, dropout=dropout)
            self.transformer = nn.TransformerEncoder(enc, n_layers)
            self.layer_norm  = nn.LayerNorm(d_model)
            self.fc1         = nn.Linear(d_model, d_model // 2)
            self.out         = nn.Linear(d_model // 2, num_classes)
            self.dropout     = nn.Dropout(dropout)

        def forward(self, x):
            batch, n_ch, freq, time = x.shape
            ch_outs = []
            for c in range(n_ch):
                cd = x[:, c:c+1, :, :]
                o1 = self.conv1(cd);  o1 = o1 * self.sa1(o1)
                o2 = self.conv2(o1);  o2 = o2 * self.sa2(o2)
                o3 = self.conv3(o2);  o3 = o3 * self.sa3(o3)
                f1 = self.pool(o1).squeeze(-1).squeeze(-1)
                f2 = self.pool(o2).squeeze(-1).squeeze(-1)
                f3 = self.pool(o3).squeeze(-1).squeeze(-1)
                ch_outs.append(torch.cat([f1, f2, f3], dim=1))
            feats = torch.stack(ch_outs, dim=1)
            t = self.pos_encoder(self.feature_fusion(feats))
            t = self.transformer(t.permute(1, 0, 2)).mean(0)
            t = self.layer_norm(t)
            return self.out(self.dropout(F.relu(self.fc1(t))))

    return CNNTransformerClassifier(num_classes=num_classes)


# ─── Handler ──────────────────────────────────────────────────────────────────

DEFAULT_LABEL_MAP = {'0': 'LPD (Lateralised Periodic Discharge)', '1': 'Seizure'}
DROP_COLS         = ['EKG']


@register('eeg_csv')
class EEGCSVHandler(InferenceHandler):

    accepted_extensions = ['parquet', 'csv']

    def validate_file(self, file, filename: str) -> None:
        super().validate_file(file, filename)
        max_mb = self.cfg.get('max_file_mb', 200)
        if hasattr(file, 'size') and file.size > max_mb * 1024 * 1024:
            raise InferenceError(
                f'File is too large ({file.size / 1e6:.0f} MB). '
                f'Maximum: {max_mb} MB.'
            )

    def load_and_preprocess(self, file, filename: str):
        try:
            if filename.endswith('.parquet'):
                df = pd.read_parquet(io.BytesIO(file.read()))
            else:
                df = pd.read_csv(file)
        except Exception as e:
            raise InferenceError(f'Could not read file: {e}')

        original_cols = list(df.columns)
        n_original    = len(df)

        drop = [c for c in DROP_COLS if c in df.columns]
        if drop:
            df = df.drop(columns=drop)

        n_channels = int(self.cfg.get('n_channels', 19))
        fs         = int(self.cfg.get('sampling_rate_hz', 200))
        nperseg    = int(self.cfg.get('nperseg', 128))

        if df.shape[1] < n_channels:
            raise InferenceError(
                f'Not enough EEG channels. Found {df.shape[1]} columns '
                f'(after dropping {drop}), model needs {n_channels}. '
                f'Your file columns: {original_cols}.'
            )

        expected_ch = self.cfg.get('expected_channels')
        if expected_ch:
            missing = [c for c in expected_ch if c not in df.columns]
            if missing:
                raise InferenceError(
                    f'Missing EEG channels: {missing}. '
                    f'Your file has: {list(df.columns)}.'
                )
            df = df[expected_ch]

        min_samples = nperseg * 2
        if len(df) < min_samples:
            raise InferenceError(
                f'File has only {len(df)} samples ({len(df)/fs:.1f} s at {fs} Hz). '
                f'Minimum: {min_samples} samples ({min_samples/fs:.1f} s).'
            )

        try:
            tensor = _preprocess_eeg(df, fs=fs, nperseg=nperseg, n_channels=n_channels)
        except InferenceError:
            raise
        except Exception as e:
            raise InferenceError(f'Preprocessing failed: {e}')

        _, n_ch, freq_bins, time_steps = tensor.shape
        input_summary = {
            'format':             'Parquet' if filename.endswith('.parquet') else 'CSV',
            'total_samples':      n_original,
            'duration_sec':       round(n_original / fs, 1),
            'sampling_rate_hz':   fs,
            'eeg_channels':       n_ch,
            'dropped_columns':    drop,
            'segment_used':       'centre 10 s',
            'spectrogram_shape':  f'{freq_bins} freq bins × {time_steps} time steps',
            'preprocessing':      f'bandpass 0.5–30 Hz → StandardScaler → STFT (nperseg={nperseg})',
        }

        self._tensor = tensor
        return pd.DataFrame([{'tensor_ready': True}]), input_summary

    def run(self, uploaded_file=None, input_data: dict | None = None) -> dict:
        import torch
        import torch.nn.functional as F

        if uploaded_file is None:
            raise InferenceError('This model requires an EEG file (CSV or Parquet).')

        filename = getattr(uploaded_file, 'name', '').lower()
        self.validate_file(uploaded_file, filename)
        feature_df, input_summary = self.load_and_preprocess(uploaded_file, filename)
        tensor = self._tensor

        # ── Load model ────────────────────────────────────────────────────────
        if not self.ai_model.model_file:
            raise InferenceError(
                'No model file uploaded. Upload a .pth/.pt file via Django admin.'
            )
        model_path = self.ai_model.model_file.path
        if not os.path.exists(model_path):
            raise InferenceError(
                f'Model file not found on disk: {os.path.basename(model_path)}. '
                'Re-upload via Django admin.'
            )

        variant     = self.cfg.get('model_variant', 'cnn_simple')
        label_map   = {**DEFAULT_LABEL_MAP, **self.cfg.get('label_map', {})}
        num_classes = len(label_map)

        try:
            if variant == 'cnn_transformer':
                model = _build_cnn_transformer(num_classes=num_classes)
            else:
                model = _build_cnn_simple(num_classes=num_classes)

            state_dict = torch.load(model_path, map_location='cpu', weights_only=True)
            model.load_state_dict(state_dict)
        except RuntimeError as e:
            raise InferenceError(
                f'Model weights do not match the "{variant}" architecture. '
                f'Detail: {e}. '
                'If you use the other variant, set model_variant in handler_config.'
            )
        except Exception as e:
            raise InferenceError(f'Failed to load model: {e}')

        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        model.to(device).eval()

        # ── Inference ─────────────────────────────────────────────────────────
        x = torch.tensor(tensor, dtype=torch.float32).to(device)
        with torch.no_grad():
            logits     = model(x)
            probs      = F.softmax(logits, dim=1)
            pred_idx   = int(torch.argmax(probs, dim=1).item())
            confidence = float(probs[0, pred_idx].item())

        label = label_map.get(str(pred_idx), str(pred_idx))

        spec_sample = tensor[0, 0, :, :5].flatten()[:20]
        return {
            'success':          True,
            'prediction':       pred_idx,
            'prediction_label': label,
            'prediction_proba': confidence,
            'risk_score':       confidence,
            'label':            label,
            'input_summary':    input_summary,
            'input_data': {
                **{f'ch0_spec_f{i}': round(float(v), 5)
                   for i, v in enumerate(spec_sample)},
                'n_channels':    tensor.shape[1],
                'freq_bins':     tensor.shape[2],
                'time_steps':    tensor.shape[3],
                'predicted_idx': pred_idx,
                'confidence':    round(confidence, 4),
            },
        }

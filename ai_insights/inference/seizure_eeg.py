# ai_insights/inference/seizure_eeg.py
"""
Inference handler for EEG Seizure / LPD Detection.
Identical to PersonalPortfolio seizure_eeg.py — only self.project → self.ai_model.

Pipeline
--------
1.  Read Parquet/CSV  →  drop EKG channel
2.  Take centre 10 seconds  (indices: centre±5000 at 200 Hz)
3.  Bandpass filter each channel  (0.5–30 Hz, Butterworth order 4)
4.  StandardScaler normalisation
5.  STFT spectrogram per channel  (nperseg=128, noverlap=64)
6.  Stack into tensor  (1, 19, freq_bins, time_steps)
7.  Load model  →  forward pass  →  softmax  →  predicted class

Model choices (set "model_variant" in handler_config):
  "cnn_simple"       — the architecture that matches the uploaded .pth weights
                       (Conv2d→BN→Conv2d→BN→MHA→LN→fc1→fc2)
  "cnn_transformer"  — the full CNN_Transformer_Classifier from the paper

Admin config (handler_config):
{
  "handler":           "seizure_eeg",
  "model_variant":     "cnn_simple",
  "sampling_rate_hz":  200,
  "n_channels":        19,
  "nperseg":           128,
  "label_map":         {"0": "LPD", "1": "Seizure"}
}
"""

import os
import io
import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt, stft as scipy_stft
from sklearn.preprocessing import StandardScaler

from .registry import register
from .base import InferenceHandler, InferenceError


# ─────────────────────────────────────────────────────────────────────────────
# Signal processing  (matches CNNDataset exactly)
# ─────────────────────────────────────────────────────────────────────────────

def _butter_bandpass(lowcut=0.5, highcut=30.0, fs=200, order=4):
    nyq = 0.5 * fs
    b, a = butter(order, [lowcut / nyq, highcut / nyq], btype='band')
    return b, a


def _bandpass_filter(data: np.ndarray, lowcut=0.5, highcut=30.0, fs=200, order=4) -> np.ndarray:
    b, a = _butter_bandpass(lowcut, highcut, fs, order)
    return filtfilt(b, a, data)


def _create_stft_spectrogram(signal: np.ndarray, fs: int = 200,
                              nperseg: int = 128, noverlap: int = None) -> np.ndarray:
    """Matches create_stft_spectrogram from training code exactly."""
    if noverlap is None:
        noverlap = nperseg // 2
    if len(signal) < nperseg:
        raise InferenceError(
            f"Signal segment too short for STFT: {len(signal)} samples, "
            f"need at least {nperseg}."
        )
    _, _, spec = scipy_stft(signal, fs=fs, nperseg=nperseg, noverlap=noverlap)
    spec = np.abs(spec)
    mn, mx = spec.min(), spec.max()
    if mx > mn:
        spec = (spec - mn) / (mx - mn)
    spec = np.log1p(spec)
    return spec  # shape: (freq_bins, time_steps)


def _preprocess_eeg(df: pd.DataFrame, fs: int = 200, nperseg: int = 128,
                    n_channels: int = 19) -> np.ndarray:
    """
    Full preprocessing pipeline:
      raw DataFrame → bandpass → scale → STFT per channel
    Returns tensor numpy array of shape (1, n_channels, freq_bins, time_steps).
    """
    # ── 1. Centre crop: 10 seconds = 2000 samples at 200 Hz ──────────────────
    total_samples = len(df)
    centre = total_samples // 2
    start  = centre - 1000   # 5 seconds before centre
    end    = centre + 1000   # 5 seconds after  centre

    # Clamp to available data
    start = max(start, 0)
    end   = min(end,   total_samples)

    if (end - start) < nperseg:
        raise InferenceError(
            f"File has only {total_samples} samples — too short. "
            f"Need at least {nperseg} samples (≈ {nperseg/fs:.1f} s at {fs} Hz). "
            f"For best results upload ≥ 10 seconds of EEG ({10*fs} samples)."
        )

    segment = df.iloc[start:end, :n_channels].copy()

    # ── 2. Bandpass filter each channel ──────────────────────────────────────
    filtered = pd.DataFrame(
        {col: _bandpass_filter(segment[col].values, fs=fs) for col in segment.columns},
        index=segment.index,
    )

    # ── 3. Standardise ────────────────────────────────────────────────────────
    scaler   = StandardScaler()
    scaled   = pd.DataFrame(
        scaler.fit_transform(filtered),
        columns=filtered.columns,
    )

    # ── 4. STFT spectrogram per channel ───────────────────────────────────────
    spectrograms = []
    for i in range(min(n_channels, scaled.shape[1])):
        ch_data = scaled.iloc[:, i].values
        spec    = _create_stft_spectrogram(ch_data, fs=fs, nperseg=nperseg)
        spectrograms.append(spec)  # (freq_bins, time_steps)

    if len(spectrograms) < n_channels:
        raise InferenceError(
            f"File has {scaled.shape[1]} EEG channels, but the model expects {n_channels}. "
            "Check that you haven't accidentally dropped channels before uploading."
        )

    # shape: (1, n_channels, freq_bins, time_steps)
    tensor = np.stack(spectrograms, axis=0)[np.newaxis, ...]
    return tensor.astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# Model architectures
# ─────────────────────────────────────────────────────────────────────────────

def _build_cnn_simple(num_classes: int = 2):
    """
    Reconstructed from the actual .pth weights (cnn_transformer_best_model90_22.pth).

    Weights analysis:
      conv1.0.weight : (64, 1,  7, 7)  → Conv2d(1,  64,  7, 7)
      conv2.0.weight : (128,64, 5, 5)  → Conv2d(64, 128, 5, 5)
      self_attention.in_proj_weight : (384, 128) → MHA embed_dim=128
      fc1.weight     : (64, 128)  → Linear(128, 64)
      fc2.weight     : (2,  64)   → Linear(64, 2)

    Input shape expected: (batch, 1, freq_bins, time_steps)
    — the model processes all channels flattened into the batch dimension,
      then pools back. See forward() below.
    """
    import torch.nn as nn
    import torch

    class CNNSimple(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Sequential(
                nn.Conv2d(1, 64, kernel_size=7, padding=3),
                nn.BatchNorm2d(64),
                nn.GELU(),
                nn.MaxPool2d(2, 2),
            )
            self.conv2 = nn.Sequential(
                nn.Conv2d(64, 128, kernel_size=5, padding=2),
                nn.BatchNorm2d(128),
                nn.GELU(),
                nn.MaxPool2d(2, 2),
            )
            self.global_pool    = nn.AdaptiveAvgPool2d((1, 1))
            self.self_attention = nn.MultiheadAttention(embed_dim=128, num_heads=4,
                                                        batch_first=True)
            self.layer_norm     = nn.LayerNorm(128)
            self.fc1            = nn.Linear(128, 64)
            self.fc2            = nn.Linear(64, num_classes)
            self.dropout        = nn.Dropout(0.3)

        def forward(self, x):
            # x: (batch, n_channels, freq, time)
            batch, n_ch, freq, time = x.shape

            # Process every channel independently through CNN
            ch_features = []
            for c in range(n_ch):
                ch = x[:, c:c+1, :, :]           # (batch, 1, freq, time)
                ch = self.conv1(ch)               # (batch, 64, ...)
                ch = self.conv2(ch)               # (batch, 128, ...)
                ch = self.global_pool(ch)         # (batch, 128, 1, 1)
                ch = ch.squeeze(-1).squeeze(-1)   # (batch, 128)
                ch_features.append(ch)

            # Stack → sequence for attention: (batch, n_channels, 128)
            seq = torch.stack(ch_features, dim=1)
            attn_out, _ = self.self_attention(seq, seq, seq)
            out = self.layer_norm(attn_out.mean(dim=1))  # (batch, 128)

            out = self.dropout(torch.relu(self.fc1(out)))
            return self.fc2(out)

    return CNNSimple()


def _build_cnn_transformer(num_classes: int = 2):
    """
    Full CNN_Transformer_Classifier — use with future weights that match this architecture.
    """
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class PositionalEncoding(nn.Module):
        def __init__(self, d_model, max_len=5000):
            super().__init__()
            if d_model % 2 != 0:
                d_model += 1
            pe = torch.zeros(max_len, d_model)
            pos = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
            div = torch.exp(torch.arange(0, d_model, 2).float() * -(np.log(10000.0) / d_model))
            pe[:, 0::2] = torch.sin(pos * div)
            pe[:, 1::2] = torch.cos(pos * div)
            self.register_buffer('pe', pe.unsqueeze(0))

        def forward(self, x):
            return x + self.pe[:, :x.size(1), :x.size(2)]

    class CNNTransformerClassifier(nn.Module):
        def __init__(self, in_channels=1,
                     cnn_out_channels=(32, 16, 8),
                     cnn_kernel_sizes=((5,5),(3,3),(3,3)),
                     cnn_paddings=((1,1),(2,2),(3,3)),
                     d_model=100, num_heads=4,
                     num_transformer_layers=2, dropout_rate=0.5,
                     num_classes=2, num_eeg_channels=19):
            super().__init__()
            self.num_eeg_channels = num_eeg_channels

            def conv_block(in_ch, out_ch, k, p):
                return nn.Sequential(
                    nn.Conv2d(in_ch, out_ch, k, padding=p),
                    nn.BatchNorm2d(out_ch), nn.GELU(),
                    nn.MaxPool2d(2, 2),
                )

            self.conv1 = conv_block(in_channels, cnn_out_channels[0],
                                    cnn_kernel_sizes[0], cnn_paddings[0])
            self.conv2 = conv_block(cnn_out_channels[0], cnn_out_channels[1],
                                    cnn_kernel_sizes[1], cnn_paddings[1])
            self.conv3 = nn.Sequential(
                nn.Conv2d(cnn_out_channels[1], cnn_out_channels[2],
                          cnn_kernel_sizes[2], padding=cnn_paddings[2]),
                nn.BatchNorm2d(cnn_out_channels[2]), nn.GELU(),
            )
            self.sa1 = nn.Sequential(nn.Conv2d(cnn_out_channels[0], 1, 1), nn.Sigmoid())
            self.sa2 = nn.Sequential(nn.Conv2d(cnn_out_channels[1], 1, 1), nn.Sigmoid())
            self.sa3 = nn.Sequential(nn.Conv2d(cnn_out_channels[2], 1, 1), nn.Sigmoid())
            self.pool = nn.AdaptiveAvgPool2d((1, 1))
            self.feature_fusion = nn.Linear(sum(cnn_out_channels), d_model)
            self.pos_encoder = PositionalEncoding(d_model)
            enc_layer = nn.TransformerEncoderLayer(d_model, num_heads,
                                                   dropout=dropout_rate)
            self.transformer  = nn.TransformerEncoder(enc_layer, num_transformer_layers)
            self.layer_norm   = nn.LayerNorm(d_model)
            self.fc1          = nn.Linear(d_model, d_model // 2)
            self.out          = nn.Linear(d_model // 2, num_classes)
            self.dropout      = nn.Dropout(dropout_rate)

        def forward(self, x):
            batch, n_ch, freq, time = x.shape
            ch_outs = []
            for c in range(n_ch):
                cd = x[:, c:c+1, :, :]
                o1 = self.conv1(cd);   o1 = o1 * self.sa1(o1)
                o2 = self.conv2(o1);   o2 = o2 * self.sa2(o2)
                o3 = self.conv3(o2);   o3 = o3 * self.sa3(o3)
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


# ─────────────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────────────

@register("seizure_eeg")
class SeizureEEGHandler(InferenceHandler):

    accepted_extensions = ["parquet", "csv"]

    # Columns to drop before processing (non-EEG channels)
    DROP_COLS = ["EKG"]

    # Class labels matching LabelEncoder.fit(['Seizure', 'LPD'])
    # LabelEncoder sorts alphabetically: LPD=0, Seizure=1
    DEFAULT_LABEL_MAP = {"0": "LPD (Lateralised Periodic Discharge)",
                         "1": "Seizure"}

    def validate_file(self, file, filename: str) -> None:
        super().validate_file(file, filename)
        max_mb = self.cfg.get("max_file_mb", 200)
        if hasattr(file, "size") and file.size > max_mb * 1024 * 1024:
            raise InferenceError(
                f"File is too large ({file.size / 1e6:.0f} MB). "
                f"Maximum allowed: {max_mb} MB."
            )

    def load_and_preprocess(self, file, filename: str):
        # ── 1. Parse file ─────────────────────────────────────────────────────
        try:
            if filename.endswith(".parquet"):
                df = pd.read_parquet(io.BytesIO(file.read()))
            else:
                df = pd.read_csv(file)
        except Exception as e:
            raise InferenceError(f"Could not read file: {e}")

        original_cols = list(df.columns)
        n_original    = len(df)

        # ── 2. Drop non-EEG columns ───────────────────────────────────────────
        drop = [c for c in self.DROP_COLS if c in df.columns]
        if drop:
            df = df.drop(columns=drop)

        # ── 3. Validate channel count ─────────────────────────────────────────
        n_channels = self.cfg.get("n_channels", 19)
        fs         = int(self.cfg.get("sampling_rate_hz", 200))
        nperseg    = int(self.cfg.get("nperseg", 128))

        if df.shape[1] < n_channels:
            raise InferenceError(
                f"Not enough EEG channels. Found {df.shape[1]} columns "
                f"(after dropping {drop}), but the model needs {n_channels}. "
                f"Your file columns: {original_cols}."
            )

        # Optionally validate specific channel names
        expected_ch = self.cfg.get("expected_channels")
        if expected_ch:
            missing = [c for c in expected_ch if c not in df.columns]
            if missing:
                raise InferenceError(
                    f"Missing EEG channels: {missing}. "
                    f"Your file has: {list(df.columns)}."
                )
            df = df[expected_ch]

        # ── 4. Sample-count sanity check ──────────────────────────────────────
        min_samples = nperseg * 2
        if len(df) < min_samples:
            raise InferenceError(
                f"File has only {len(df)} samples "
                f"({len(df)/fs:.1f} s at {fs} Hz). "
                f"Minimum required: {min_samples} samples "
                f"({min_samples/fs:.1f} s). "
                "Please upload a longer EEG segment."
            )

        # ── 5. Run preprocessing pipeline ────────────────────────────────────
        try:
            tensor = _preprocess_eeg(df, fs=fs, nperseg=nperseg,
                                     n_channels=n_channels)
        except InferenceError:
            raise
        except Exception as e:
            raise InferenceError(f"Preprocessing failed: {e}")

        # ── 6. Build input summary shown in the UI ────────────────────────────
        _, n_ch, freq_bins, time_steps = tensor.shape
        input_summary = {
            "format":          "Parquet" if filename.endswith(".parquet") else "CSV",
            "total_samples":   n_original,
            "duration_sec":    round(n_original / fs, 1),
            "sampling_rate_hz": fs,
            "eeg_channels":    n_ch,
            "dropped_columns": drop,
            "segment_used":    "centre 10 s",
            "spectrogram_shape": f"{freq_bins} freq bins × {time_steps} time steps",
            "preprocessing":   f"bandpass 0.5–30 Hz → StandardScaler → STFT (nperseg={nperseg})",
        }

        self._tensor = tensor
        placeholder = pd.DataFrame([{"tensor_ready": True}])
        return placeholder, input_summary

    def postprocess(self, raw_prediction, proba, feature_df):
        label_map  = {**self.DEFAULT_LABEL_MAP, **self.cfg.get("label_map", {})}
        pred_val   = self._pred_val
        confidence = self._proba
        label      = label_map.get(str(pred_val), str(pred_val))
        return {
            "prediction":       pred_val,
            "prediction_label": label,
            "prediction_proba": confidence,
        }

    def run(self, uploaded_file=None, input_data: dict | None = None) -> dict:
        """
        Override base run() to do the full PyTorch inference.
        Identical to PersonalPortfolio — only self.project → self.ai_model.
        """
        import torch
        import torch.nn.functional as F

        if uploaded_file is None:
            raise InferenceError("This model requires an EEG file (CSV or Parquet).")

        filename = uploaded_file.name.lower()

        # ── Validate + preprocess ─────────────────────────────────────────────
        self.validate_file(uploaded_file, filename)
        feature_df, input_summary = self.load_and_preprocess(uploaded_file, filename)
        tensor = self._tensor  # shape: (1, n_channels, freq_bins, time_steps)

        # ── Load model ────────────────────────────────────────────────────────
        if not self.ai_model.model_file:
            raise InferenceError(
                "Model file not found on the server. "
                "Please upload the trained .pth file via Django admin."
            )
        model_path = self.ai_model.model_file.path
        if not os.path.exists(model_path):
            raise InferenceError(
                "Model file not found on disk: "
                f"{os.path.basename(model_path)}. Re-upload via Django admin."
            )

        variant     = self.cfg.get("model_variant", "cnn_simple")
        num_classes = len({**self.DEFAULT_LABEL_MAP, **self.cfg.get("label_map", {})})

        try:
            if variant == "cnn_transformer":
                model = _build_cnn_transformer(num_classes=num_classes)
            else:
                model = _build_cnn_simple(num_classes=num_classes)

            state_dict = torch.load(model_path, map_location="cpu",
                                    weights_only=True)
            model.load_state_dict(state_dict)
        except RuntimeError as e:
            raise InferenceError(
                f"Model weights could not be loaded into the '{variant}' architecture. "
                f"Detail: {e}. "
                "If you recently changed the model, update 'model_variant' in handler_config."
            )
        except Exception as e:
            raise InferenceError(f"Failed to load model: {e}")

        model.eval()
        device = torch.device("cpu")
        if torch.cuda.is_available():
            device = torch.device("cuda")
        model.to(device)

        # ── Inference ─────────────────────────────────────────────────────────
        x = torch.tensor(tensor, dtype=torch.float32).to(device)

        with torch.no_grad():
            logits     = model(x)                          # (1, num_classes)
            probs      = F.softmax(logits, dim=1)          # (1, num_classes)
            pred_idx   = int(torch.argmax(probs, dim=1).item())
            confidence = float(probs[0, pred_idx].item())

        # Stash for postprocess()
        self._pred_val = pred_idx
        self._proba    = confidence

        # ── Build final result ────────────────────────────────────────────────
        result = self.postprocess(None, confidence, feature_df)
        result["success"]       = True
        result["risk_score"]    = confidence
        result["label"]         = result["prediction_label"]
        result["input_summary"] = input_summary

        spec_sample = tensor[0, 0, :, :5].flatten()[:20]
        result["input_data"] = {
            f"ch0_spec_f{i}": round(float(v), 5)
            for i, v in enumerate(spec_sample)
        }
        result["input_data"]["n_channels"]    = tensor.shape[1]
        result["input_data"]["freq_bins"]     = tensor.shape[2]
        result["input_data"]["time_steps"]    = tensor.shape[3]
        result["input_data"]["predicted_idx"] = pred_idx
        result["input_data"]["confidence"]    = round(confidence, 4)

        return result

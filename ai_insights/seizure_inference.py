"""
Standalone EEG seizure inference for HealthCompass.
Ported from PersonalPortfolio/projects/inference/seizure_eeg.py.
Runs locally — no proxy to hasanai.net.
"""
import os
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.signal import butter, filtfilt, stft as scipy_stft
from sklearn.preprocessing import StandardScaler

_MODELS_DIR = Path(__file__).parent / "models"

VARIANT_FILES = {
    "cnn_transformer": "cnn_transformer.pth",
    "gru_attention":   "gru_attention.pth",
    "fusion":          "fusion.pth",
}

LABEL_MAP = {"0": "LPD (Lateralised Periodic Discharge)", "1": "Seizure"}

_MODEL_CACHE: dict = {}


# ── Signal processing ─────────────────────────────────────────────────────────

def _bandpass(data, lowcut=0.5, highcut=30.0, fs=200, order=4):
    nyq = 0.5 * fs
    b, a = butter(order, [lowcut / nyq, highcut / nyq], btype="band")
    return filtfilt(b, a, data)


def _stft_spec(signal, fs=200, nperseg=128):
    noverlap = nperseg // 2
    _, _, spec = scipy_stft(signal, fs=fs, nperseg=nperseg, noverlap=noverlap)
    spec = np.abs(spec)
    mn, mx = spec.min(), spec.max()
    if mx > mn:
        spec = (spec - mn) / (mx - mn)
    return np.log1p(spec)


def _preprocess_cnn(df: pd.DataFrame, fs=200, nperseg=128, n_ch=19):
    total = len(df)
    c = total // 2
    seg = df.iloc[max(c - 1000, 0):min(c + 1000, total), :n_ch].copy()
    filtered = pd.DataFrame({col: _bandpass(seg[col].values, fs=fs)
                              for col in seg.columns})
    scaled = pd.DataFrame(StandardScaler().fit_transform(filtered),
                          columns=filtered.columns)
    specs = [_stft_spec(scaled.iloc[:, i].values, fs=fs, nperseg=nperseg)
             for i in range(min(n_ch, scaled.shape[1]))]
    return np.stack(specs, axis=0)[np.newaxis, ...].astype(np.float32)


def _preprocess_gru(df: pd.DataFrame, fs=200, n_ch=19):
    n_windows, window_size = 10, fs
    needed = n_windows * window_size
    total = len(df)
    c = total // 2
    s = max(c - needed // 2, 0)
    e = s + needed
    if e > total:
        s = max(total - needed, 0); e = s + needed
    seg = df.iloc[s:e, :n_ch].copy()
    scaled = StandardScaler().fit_transform(
        pd.DataFrame({col: _bandpass(seg[col].values, fs=fs)
                      for col in seg.columns})
    )
    windows = [scaled[i * window_size:(i + 1) * window_size, :n_ch].T
               for i in range(n_windows)]
    return np.stack(windows, axis=0)[np.newaxis, ...].astype(np.float32)


# ── Model architectures ───────────────────────────────────────────────────────

def _build_cnn_simple(num_classes=2):
    import torch, torch.nn as nn

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1       = nn.Sequential(nn.Conv2d(1, 64, kernel_size=7, padding=3),
                                             nn.BatchNorm2d(64), nn.GELU(), nn.MaxPool2d(2, 2))
            self.conv2       = nn.Sequential(nn.Conv2d(64, 128, kernel_size=5, padding=2),
                                             nn.BatchNorm2d(128), nn.GELU(), nn.MaxPool2d(2, 2))
            self.global_pool    = nn.AdaptiveAvgPool2d((1, 1))
            self.self_attention = nn.MultiheadAttention(embed_dim=128, num_heads=4, batch_first=True)
            self.layer_norm     = nn.LayerNorm(128)
            self.fc1            = nn.Linear(128, 64)
            self.fc2            = nn.Linear(64, num_classes)
            self.dropout        = nn.Dropout(0.3)

        def forward(self, x):
            B, C, Fr, T = x.shape
            ch_features = []
            for c in range(C):
                ch = x[:, c:c+1, :, :]
                ch = self.conv1(ch)
                ch = self.conv2(ch)
                ch = self.global_pool(ch).squeeze(-1).squeeze(-1)
                ch_features.append(ch)
            seq = torch.stack(ch_features, dim=1)
            attn_out, _ = self.self_attention(seq, seq, seq)
            out = self.layer_norm(attn_out.mean(dim=1))
            out = self.dropout(torch.relu(self.fc1(out)))
            return self.fc2(out)

    return Model()


def _build_gru_attention(state_dict, num_classes=2):
    import torch, torch.nn as nn

    ih         = state_dict["gru.weight_ih_l0"]
    gru_in     = int(ih.shape[1])
    gru_hidden = int(ih.shape[0]) // 3
    bidir      = "gru.weight_ih_l0_reverse" in state_dict
    gru_out    = gru_hidden * (2 if bidir else 1)

    has_in_norm   = "input_norm_gru.weight" in state_dict
    in_norm_is_bn = has_in_norm and "input_norm_gru.running_mean" in state_dict
    in_norm_size  = int(state_dict["input_norm_gru.weight"].shape[0]) if has_in_norm else None

    has_gru_norm   = "gru_norm.weight" in state_dict
    gru_norm_is_bn = has_gru_norm and "gru_norm.running_mean" in state_dict
    gru_norm_size  = int(state_dict["gru_norm.weight"].shape[0]) if has_gru_norm else None

    has_proj = "projection.weight" in state_dict
    proj_out = int(state_dict["projection.weight"].shape[0]) if has_proj else gru_out

    embed_dim = int(state_dict["self_attention.in_proj_weight"].shape[1])
    num_heads = 4 if embed_dim % 4 == 0 else (2 if embed_dim % 2 == 0 else 1)

    cl_in     = int(state_dict["classification_head.0.weight"].shape[1])
    cl_hidden = int(state_dict["classification_head.0.weight"].shape[0])
    use_skip  = (cl_in == 2 * embed_dim)

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            if has_in_norm:
                self.input_norm_gru = (nn.BatchNorm1d(in_norm_size) if in_norm_is_bn
                                       else nn.LayerNorm(in_norm_size))
            self.gru = nn.GRU(gru_in, gru_hidden, batch_first=True, bidirectional=bidir)
            if has_gru_norm:
                self.gru_norm = (nn.BatchNorm1d(gru_norm_size) if gru_norm_is_bn
                                 else nn.LayerNorm(gru_norm_size))
            if has_proj:
                self.projection = nn.Linear(gru_out, proj_out)
            self.self_attention = nn.MultiheadAttention(embed_dim, num_heads, batch_first=True)
            self.classification_head = nn.Sequential(
                nn.Linear(cl_in, cl_hidden), nn.LayerNorm(cl_hidden),
                nn.GELU(), nn.Dropout(0.5), nn.Linear(cl_hidden, num_classes),
            )

        def forward(self, x):
            B, S, C, F = x.shape
            x = x.permute(0, 2, 1, 3).contiguous().reshape(B * C, S, F)
            if has_in_norm:
                if in_norm_is_bn:
                    BC, Sl, Fl = x.shape
                    x = self.input_norm_gru(x.reshape(BC * Sl, Fl)).reshape(BC, Sl, Fl)
                else:
                    x = self.input_norm_gru(x)
            out, _ = self.gru(x)
            last = out[:, -1, :]
            if has_gru_norm:
                last = self.gru_norm(last)
            last = last.reshape(B, C, -1)
            if has_proj:
                last = self.projection(last)
            attn, _ = self.self_attention(last, last, last)
            pooled = (torch.cat([last.mean(1), attn.mean(1)], -1)
                      if use_skip else attn.mean(1))
            return self.classification_head(pooled)

    return Model()


def _build_fusion(num_classes=2):
    import torch, torch.nn as nn

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv_layers = nn.Sequential(
                nn.Conv2d(1, 64, 7, padding=3), nn.BatchNorm2d(64), nn.GELU(), nn.MaxPool2d(2),
                nn.Conv2d(64, 96, 5, padding=2), nn.BatchNorm2d(96), nn.GELU(), nn.MaxPool2d(2),
            )
            self.pool          = nn.AdaptiveAvgPool2d((1, 1))
            self.gru           = nn.GRU(200, 32, batch_first=True)
            self.embedding     = nn.Linear(128, 128)
            self.self_attention = nn.MultiheadAttention(128, 4, batch_first=True)
            self.layer_norm    = nn.LayerNorm(128)
            self.drop          = nn.Dropout(0.5)
            self.classifier    = nn.Sequential(nn.Linear(128, 64), nn.GELU(),
                                               nn.Dropout(0.5), nn.Linear(64, num_classes))

        def forward(self, cnnx, lstmx):
            B, H, C, W = cnnx.shape
            cx = cnnx.permute(0, 2, 1, 3).contiguous().reshape(B * C, 1, H, W)
            cx = self.pool(self.conv_layers(cx)).reshape(B, C, -1)
            Bl, S, Cl, F = lstmx.shape
            lx = lstmx.permute(0, 2, 1, 3).contiguous().reshape(Bl * Cl, S, F)
            gx, _ = self.gru(lx)
            gx = gx[:, -1, :].reshape(B, C, -1)
            fused = self.embedding(torch.cat([cx, gx], -1))
            attn, _ = self.self_attention(fused, fused, fused)
            return self.classifier(self.drop(self.layer_norm(attn.mean(1))))

    return Model()


# ── Model loader with cache ───────────────────────────────────────────────────

def _load_model(variant: str):
    import torch
    path = _MODELS_DIR / VARIANT_FILES[variant]
    if not path.exists():
        raise FileNotFoundError(f"Model file not found: {path}")
    if variant in _MODEL_CACHE:
        return _MODEL_CACHE[variant]
    sd = torch.load(str(path), map_location="cpu", weights_only=True)
    if variant == "cnn_transformer":
        model = _build_cnn_simple()
    elif variant == "gru_attention":
        model = _build_gru_attention(sd)
    elif variant == "fusion":
        model = _build_fusion()
    else:
        raise ValueError(f"Unknown variant: {variant}")
    model.load_state_dict(sd)
    model.eval()
    _MODEL_CACHE[variant] = model
    return model


# ── Public API ────────────────────────────────────────────────────────────────

def predict(data_dict: dict, variant: str = "ensemble", fs: int = 200) -> dict:
    """
    data_dict : {channel_name: [float, ...], ...}  — raw 10 s window
    variant   : 'cnn_transformer' | 'gru_attention' | 'fusion' | 'ensemble'
    Returns   : {label, confidence, variant, votes?, per_model?}
    """
    import torch
    import torch.nn.functional as F
    import time

    df = pd.DataFrame(data_dict)
    if "EKG" in df.columns:
        df = df.drop(columns=["EKG"])

    if variant == "ensemble":
        per_model = []
        for v in VARIANT_FILES:
            t0 = time.perf_counter()
            try:
                r = _infer_single(df.copy(), v, fs)
                per_model.append({
                    "variant": v, "label": r["label"],
                    "confidence": r["confidence"],
                    "inference_ms": int((time.perf_counter() - t0) * 1000),
                    "success": True,
                })
            except Exception as e:
                per_model.append({"variant": v, "success": False, "error": str(e)})

        good = [r for r in per_model if r.get("success")]
        if not good:
            raise RuntimeError("All models failed: " +
                               "; ".join(r.get("error", "?") for r in per_model))
        labels = [r["label"] for r in good]
        votes  = {l: labels.count(l) for l in set(labels)}
        winner = max(votes, key=votes.get)
        avg_conf = (sum(r["confidence"] for r in good if r["label"] == winner)
                    / votes[winner])
        return {
            "label": winner, "confidence": round(avg_conf, 4),
            "variant": "ensemble", "votes": votes, "per_model": per_model,
        }

    return _infer_single(df, variant, fs)


def _infer_single(df: pd.DataFrame, variant: str, fs: int = 200) -> dict:
    import torch, torch.nn.functional as F

    model = _load_model(variant)
    n_ch  = 19

    with torch.no_grad():
        if variant == "gru_attention":
            x = torch.tensor(_preprocess_gru(df, fs, n_ch))
            logits = model(x)

        elif variant == "fusion":
            cnn_t = _preprocess_cnn(df, fs, n_ch=n_ch)
            gru_t = _preprocess_gru(df, fs, n_ch)
            cnn_p = np.transpose(cnn_t, (0, 2, 1, 3))
            logits = model(torch.tensor(cnn_p), torch.tensor(gru_t))

        else:  # cnn_transformer
            x = torch.tensor(_preprocess_cnn(df, fs, n_ch=n_ch))
            logits = model(x)

        probs = F.softmax(logits, dim=1)
        idx   = int(torch.argmax(probs, dim=1).item())
        conf  = float(probs[0, idx].item())

    label = LABEL_MAP.get(str(idx), str(idx))
    return {"label": label, "confidence": round(conf, 4), "variant": variant}

"""
Standalone EEG seizure inference for HealthCompass.
Uses onnxruntime-cpu instead of PyTorch — same predictions, ~15 MB vs ~1.5 GB.
"""
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.signal import butter, filtfilt, stft as scipy_stft
from sklearn.preprocessing import StandardScaler

_MODELS_DIR = Path(__file__).parent / "models"

ONNX_FILES = {
    "cnn_transformer": "cnn_transformer.onnx",
    "gru_attention":   "gru_attention.onnx",
    "fusion":          "fusion.onnx",
}

LABEL_MAP = {"0": "LPD (Lateralised Periodic Discharge)", "1": "Seizure"}

_SESSION_CACHE: dict = {}


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


# ── ONNX session loader ───────────────────────────────────────────────────────

def _get_session(variant: str):
    if variant not in _SESSION_CACHE:
        import onnxruntime as ort
        path = _MODELS_DIR / ONNX_FILES[variant]
        if not path.exists():
            raise FileNotFoundError(f"ONNX model not found: {path}")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        _SESSION_CACHE[variant] = ort.InferenceSession(
            str(path), sess_options=opts, providers=['CPUExecutionProvider']
        )
    return _SESSION_CACHE[variant]


# ── Softmax helper ────────────────────────────────────────────────────────────

def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


# ── Public API ────────────────────────────────────────────────────────────────

def predict(data_dict: dict, variant: str = "ensemble", fs: int = 200) -> dict:
    import time
    df = pd.DataFrame(data_dict)
    if "EKG" in df.columns:
        df = df.drop(columns=["EKG"])

    if variant == "ensemble":
        per_model = []
        for v in ONNX_FILES:
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
    session = _get_session(variant)
    n_ch = 19

    if variant == "gru_attention":
        x = _preprocess_gru(df, fs, n_ch)
        logits = session.run(['logits'], {'x': x})[0]

    elif variant == "fusion":
        cnn_t = _preprocess_cnn(df, fs, n_ch=n_ch)
        gru_t = _preprocess_gru(df, fs, n_ch)
        cnn_p = np.transpose(cnn_t, (0, 2, 1, 3))
        logits = session.run(['logits'], {'cnnx': cnn_p, 'lstmx': gru_t})[0]

    else:  # cnn_transformer
        x = _preprocess_cnn(df, fs, n_ch=n_ch)
        logits = session.run(['logits'], {'x': x})[0]

    probs = _softmax(logits)
    idx   = int(np.argmax(probs[0]))
    conf  = float(probs[0, idx])

    label = LABEL_MAP.get(str(idx), str(idx))
    return {"label": label, "confidence": round(conf, 4), "variant": variant}

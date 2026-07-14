# Admin handler_config examples

Copy-paste these into the **Handler Config (JSON)** field in Django admin.

---

## `tabular_passthrough` — CSV / Parquet tabular data

```json
{
    "label_map": {
        "0": "Low Risk",
        "1": "Moderate Risk",
        "2": "High Risk"
    },
    "expected_n_features": 12,
    "target_columns": ["age", "glucose", "bmi", "blood_pressure"],
    "drop_columns": ["id", "patient_name", "date"]
}
```

| Key | Required | Description |
|---|---|---|
| `label_map` | No | Maps numeric prediction to human label |
| `expected_n_features` | No | Validates column count before inference |
| `target_columns` | No | Keep only these columns (in this order) |
| `drop_columns` | No | Remove these columns before inference |

---

## `eeg_csv` — EEG / physiological signals (CSV or EDF)

```json
{
    "handler": "eeg_csv",
    "sampling_rate_hz": 256,
    "n_channels": 19,
    "lowcut_hz": 0.5,
    "highcut_hz": 30.0,
    "nperseg": 128,
    "model_variant": "cnn_transformer",
    "label_map": {
        "0": "No seizure detected",
        "1": "Seizure detected"
    }
}
```

| Key | Required | Description |
|---|---|---|
| `sampling_rate_hz` | Yes | EEG sampling frequency in Hz |
| `n_channels` | Yes | Number of EEG channels (columns used) |
| `lowcut_hz` | No | Bandpass low cutoff (default 0.5 Hz) |
| `highcut_hz` | No | Bandpass high cutoff (default 30.0 Hz) |
| `nperseg` | No | STFT window length (default 128) |
| `label_map` | No | Maps class index → human label |

**Model file format:** `.pt` or `.pth` PyTorch model.
Expected input shape: `(batch, n_channels, freq_bins, time_steps)`

---

## `image_classifier` — JPG / PNG image classification

```json
{
    "target_size": [224, 224],
    "normalize": true,
    "channels": 3,
    "label_map": {
        "0": "Normal",
        "1": "Abnormal — consult your doctor"
    }
}
```

Chest X-ray binary classification example:
```json
{
    "target_size": [256, 256],
    "normalize": true,
    "channels": 1,
    "label_map": {
        "0": "No pneumonia detected",
        "1": "Possible pneumonia — please see a doctor"
    }
}
```

| Key | Required | Description |
|---|---|---|
| `target_size` | Yes | `[width, height]` to resize image to |
| `normalize` | No | Divide pixels by 255 (default: true) |
| `channels` | No | 1=grayscale, 3=RGB (default: 3) |
| `label_map` | No | Maps class index → human label |

**Model file format:** `.onnx`. Convert from Keras/TensorFlow with `convert_to_onnx.py`.
Expected input shape: `(batch, height, width, channels)`

---

## Diabetes risk (tabular, sklearn)

```json
{
    "label_map": {
        "0": "Low diabetes risk",
        "1": "High diabetes risk — please consult your doctor"
    },
    "expected_n_features": 8
}
```

Input schema for this model:
```json
{
    "pregnancies": "integer",
    "glucose": "float (mg/dL)",
    "blood_pressure": "float (mmHg)",
    "skin_thickness": "float (mm)",
    "insulin": "float (μU/mL)",
    "bmi": "float (kg/m²)",
    "diabetes_pedigree": "float",
    "age": "integer (years)"
}
```

---

## Heart disease risk (tabular, sklearn)

```json
{
    "label_map": {
        "0": "Low cardiovascular risk",
        "1": "Elevated cardiovascular risk — consult your cardiologist"
    },
    "expected_n_features": 13
}
```

# How to add a new inference handler

Every model type lives in exactly **one Python file** inside `ai_insights/inference/`.
No changes to views, URLs, or admin logic are needed.

---

## Step 1 — Create your handler file

```python
# ai_insights/inference/my_model.py
from .registry import register
from .base import InferenceHandler, InferenceError

@register("my_model")          # <-- this slug goes in the admin
class MyModelHandler(InferenceHandler):

    accepted_extensions = ['csv']   # shown in UI + validated on upload

    def load_and_preprocess(self, file, filename: str):
        """Read the uploaded file, return (feature_df, input_summary_dict)."""
        df = self.read_csv(file)
        summary = {'rows': len(df), 'columns': list(df.columns)}
        return df, summary

    # postprocess() is inherited — uses label_map from handler_config.
    # Override if you need custom logic:
    def postprocess(self, raw_pred, proba, feature_df):
        pred = int(raw_pred[0])
        label = self.cfg.get('label_map', {}).get(str(pred), str(pred))
        return {
            'prediction':       pred,
            'prediction_label': label,
            'prediction_proba': proba,
        }
```

## Step 2 — Register it in `__init__.py`

Add one import line:

```python
# ai_insights/inference/__init__.py
from . import my_model   # noqa: F401
```

## Step 3 — Configure the model in Django admin

1. Go to **Admin → AI Insights → AI Models → [your model]**
2. Set **Handler slug** = `my_model`
3. Set **Handler config** (JSON):
   ```json
   {
       "label_map": {"0": "Healthy", "1": "At risk"},
       "expected_n_features": 10
   }
   ```
4. Upload your `.onnx` model file. (For EEG/PyTorch handlers that override `run()`, upload a `.pt`/`.pth` weights-only file instead.)

---

## `run()` override (for PyTorch / custom pipelines)

If the default sklearn-style `model.predict(df)` flow doesn't fit your model,
override `run()` entirely:

```python
def run(self, uploaded_file=None, input_data=None):
    if uploaded_file is None:
        raise InferenceError('This model requires a file upload.')
    filename = getattr(uploaded_file, 'name', '').lower()
    self.validate_file(uploaded_file, filename)
    _, summary = self.load_and_preprocess(uploaded_file, filename)
    model = self._load_model()          # loads .onnx via onnxruntime

    # ... custom inference ...

    return {
        'success':          True,
        'prediction':       pred_class,
        'prediction_label': label,
        'risk_score':       probability,
        'label':            label,
        'input_summary':    summary,
        'input_data':       {},
    }
```

---

## Base class helpers

| Method | Description |
|---|---|
| `self.read_csv(file)` | Returns `pd.DataFrame` or raises `InferenceError` |
| `self.read_parquet(file)` | Same for Parquet |
| `self.read_image(file, target_size=(W,H))` | Returns `np.ndarray` (H,W,3) |
| `self.read_edf(file)` | Returns `(df, summary_dict)` via MNE |
| `self._load_model()` | Loads `.onnx` from `ai_model.model_file` via onnxruntime |
| `self._validate_features(df)` | Checks column count vs `cfg["expected_n_features"]` |
| `self.cfg` | Dict from `ai_model.handler_config` (set in admin) |
| `self.ai_model` | The `AIModel` instance |

---

## Error handling

- Raise `InferenceError("message")` for user-visible errors (wrong file, bad columns, etc.)
- Raise regular `Exception` for server-side bugs (will be caught and logged)

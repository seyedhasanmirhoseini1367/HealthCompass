"""
Model runner: loads a .pkl or .h5 model file and runs prediction.
Supports tabular, image, EEG/CSV, and parquet file inputs.
After prediction, calls Gemini to generate a patient-friendly interpretation.
"""
import io
import logging
import numpy as np

logger = logging.getLogger(__name__)


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_model(ai_model, input_data: dict, input_file=None) -> dict:
    """
    Run prediction. Returns:
      success, prediction, risk_score, label, error (optional), demo (optional)
    """
    itype = ai_model.input_type

    try:
        if not ai_model.model_file:
            result = _rule_based_fallback(ai_model, input_data, input_file)
        elif itype == 'tabular':
            result = _run_tabular(ai_model, input_data)
        elif itype == 'image':
            result = _run_image(ai_model, input_file)
        elif itype in ('eeg_csv', 'parquet', 'file'):
            result = _run_file_input(ai_model, input_file, itype)
        else:
            result = _run_tabular(ai_model, input_data)
    except Exception as e:
        logger.exception(f'Model run failed for {ai_model.name}: {e}')
        return {'success': False, 'error': str(e)}

    return result


# ─── Tabular (ONNX only) ──────────────────────────────────────────────────────

_BLOCKED_FORMATS = {'pkl', 'pickle', 'h5', 'keras', 'joblib'}

def _blocked_format_error(ext: str) -> dict:
    return {
        'success': False,
        'error': (
            f'.{ext} models are not supported for security reasons '
            '(pickle/Keras Lambda layers allow arbitrary code execution on the server). '
            'Please convert your model to ONNX format and re-upload. '
            'Use: python convert_to_onnx.py  (included in this project).'
        ),
    }


def _run_tabular(ai_model, input_data: dict) -> dict:
    file_path = ai_model.model_file.path
    ext = file_path.lower().rsplit('.', 1)[-1]

    if ext in _BLOCKED_FORMATS:
        return _blocked_format_error(ext)

    if ext == 'onnx':
        try:
            import onnxruntime as ort
        except ImportError:
            return {'success': False, 'error': 'onnxruntime is not installed on this server.'}
        sess = ort.InferenceSession(file_path, providers=['CPUExecutionProvider'])
        X = _build_feature_array(ai_model.input_schema, input_data)
        input_name = sess.get_inputs()[0].name
        outputs = sess.run(None, {input_name: X.astype(np.float32)})
        # outputs[0]: predicted class array; outputs[1] (if present): probability dict
        pred = int(outputs[0][0]) if len(outputs) > 0 else 0
        proba = None
        if len(outputs) > 1 and isinstance(outputs[1], list) and outputs[1]:
            prob_map = outputs[1][0]
            if isinstance(prob_map, dict):
                proba = float(max(prob_map.values()))
            elif hasattr(prob_map, 'max'):
                proba = float(prob_map.max())
        return {
            'success': True,
            'prediction': pred,
            'risk_score': proba,
            'label': _interpret(proba if proba is not None else pred, ai_model.output_schema),
        }

    elif ext in ('pt', 'pth'):
        return {
            'success': False,
            'error': (
                'PyTorch (.pth/.pt) models require a named handler. '
                'Go to Django Admin → AI Models → this model and set '
                'Handler slug to "seizure_eeg" (for EEG) or the appropriate handler slug.'
            ),
        }

    return {'success': False, 'error': f'Unsupported model format: .{ext}. Only ONNX is accepted.'}


# ─── Image input ──────────────────────────────────────────────────────────────

def _run_image(ai_model, input_file) -> dict:
    if input_file is None:
        return {'success': False, 'error': 'No image file provided.'}

    file_path = ai_model.model_file.path
    ext = file_path.lower().rsplit('.', 1)[-1]

    # Read image bytes
    img_bytes = input_file.read()
    input_file.seek(0)

    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')

    if ext in _BLOCKED_FORMATS:
        return _blocked_format_error(ext)

    if ext == 'onnx':
        try:
            import onnxruntime as ort
        except ImportError:
            return {'success': False, 'error': 'onnxruntime is not installed on this server.'}
        sess = ort.InferenceSession(file_path, providers=['CPUExecutionProvider'])
        inp  = sess.get_inputs()[0]
        # Derive expected H×W from model input shape e.g. [1, 3, 224, 224] or [1, 224, 224, 3]
        shape = inp.shape
        h = int(shape[2]) if len(shape) == 4 and shape[2] not in (None, 0) else 224
        w = int(shape[3]) if len(shape) == 4 and shape[3] not in (None, 0) else 224
        img_resized = img.resize((w, h))
        X = np.array(img_resized, dtype=np.float32) / 255.0
        # NCHW vs NHWC
        if len(shape) == 4 and shape[1] in (1, 3):
            X = np.transpose(X, (2, 0, 1))
        X = np.expand_dims(X, 0)
        outputs = sess.run(None, {inp.name: X})
        out = np.array(outputs[0][0])
        risk = float(out.max())
        idx  = int(out.argmax())
        labels = ai_model.output_schema.get('labels', {})
        label  = labels.get(str(idx), f'Class {idx}') if labels else _interpret(risk, ai_model.output_schema)
        return {
            'success': True,
            'prediction': _serialize(out.tolist()),
            'risk_score': risk,
            'label': label,
            'input_summary': f'Image {img.size[0]}×{img.size[1]} px',
        }

    return {'success': False, 'error': f'Unsupported model format for image: .{ext}. Only ONNX is accepted.'}


# ─── EEG / CSV / Parquet file input ───────────────────────────────────────────

def _run_file_input(ai_model, input_file, itype: str) -> dict:
    if input_file is None:
        return {'success': False, 'error': 'No file provided.'}

    import pandas as pd

    file_bytes = input_file.read()
    input_file.seek(0)

    fname = getattr(input_file, 'name', '') or ''
    try:
        if fname.endswith('.parquet') or itype == 'parquet':
            try:
                df = pd.read_parquet(io.BytesIO(file_bytes))
            except Exception:
                # parquet engine not installed — fall back to CSV
                df = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = pd.read_csv(io.BytesIO(file_bytes))
    except Exception as e:
        return {'success': False, 'error': f'Could not read file: {e}'}

    rows, cols = df.shape
    input_summary = f'{rows} rows × {cols} columns'

    file_path = ai_model.model_file.path
    ext = file_path.lower().rsplit('.', 1)[-1]

    # Convert to numpy, dropping non-numeric columns
    X_df = df.select_dtypes(include=[np.number])
    X = X_df.values.astype(np.float32)

    if ext in _BLOCKED_FORMATS:
        return _blocked_format_error(ext)

    if ext == 'onnx':
        try:
            import onnxruntime as ort
        except ImportError:
            return {'success': False, 'error': 'onnxruntime is not installed on this server.'}
        sess = ort.InferenceSession(file_path, providers=['CPUExecutionProvider'])
        input_name = sess.get_inputs()[0].name
        expected   = sess.get_inputs()[0].shape[1] if len(sess.get_inputs()[0].shape) > 1 else None
        row = X[:1]
        if expected and row.shape[1] != expected:
            if row.shape[1] > expected:
                row = row[:, :expected]
            else:
                row = np.pad(row, ((0, 0), (0, expected - row.shape[1])))
        outputs = sess.run(None, {input_name: row.astype(np.float32)})
        pred = int(outputs[0][0]) if len(outputs) > 0 else 0
        proba = None
        if len(outputs) > 1 and isinstance(outputs[1], list) and outputs[1]:
            prob_map = outputs[1][0]
            if isinstance(prob_map, dict):
                proba = float(max(prob_map.values()))
            elif hasattr(prob_map, 'max'):
                proba = float(prob_map.max())
        return {
            'success': True,
            'prediction': pred,
            'risk_score': proba,
            'label': _interpret(proba if proba is not None else pred, ai_model.output_schema),
            'input_summary': input_summary,
        }

    elif ext in ('pt', 'pth'):
        return {
            'success': False,
            'error': (
                'PyTorch (.pth/.pt) models require a named handler. '
                'Go to Django Admin → AI Models → this model and set '
                'Handler slug to "seizure_eeg" (for EEG) or the appropriate handler slug.'
            ),
        }

    return {'success': False, 'error': f'Unsupported model format: .{ext}. Only ONNX is accepted.'}


# ─── AI Interpretation ────────────────────────────────────────────────────────

def generate_interpretation(ai_model, result: dict, input_data: dict) -> str:
    """
    Generate a patient-friendly interpretation of the prediction result.
    Tries Groq → Gemini → static fallback.
    """
    from django.conf import settings

    guide = ai_model.interpretation_guide.strip()
    if not guide:
        guide = f"Model: {ai_model.name}. Category: {ai_model.get_category_display()}. {ai_model.description}"

    label    = result.get('label', 'unknown')
    risk     = result.get('risk_score')
    risk_pct = f"{risk*100:.0f}%" if risk is not None else 'N/A'
    demo     = result.get('demo', False)

    inputs_str = ', '.join(f'{k}={v}' for k, v in input_data.items() if k != 'csrfmiddlewaretoken') if input_data else ''
    raw_summary = result.get('input_summary', '')
    file_summary = ', '.join(f'{k}: {v}' for k, v in raw_summary.items()) if isinstance(raw_summary, dict) else str(raw_summary or '')

    prompt = f"""You are a compassionate medical AI assistant explaining a health risk assessment result to a patient.

MODEL CONTEXT:
{guide}

PREDICTION RESULT:
- Risk label: {label}
- Risk score: {risk_pct}
{'- Input values: ' + inputs_str if inputs_str else ''}
{'- Input data: ' + file_summary if file_summary else ''}
{'- Note: this is a demo/rule-based result, not from a real trained model.' if demo else ''}

Write a concise, empathetic 3-5 sentence interpretation for the patient. Include:
1. What the result means in plain language
2. What factors contributed most (if inputs are provided)
3. A clear, actionable next step
4. A reminder that this is not a medical diagnosis

Do NOT use markdown. Write in plain paragraphs."""

    # ── Try Groq first ────────────────────────────────────────────────────────
    groq_key = getattr(settings, 'GROQ_API_KEY', '')
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            resp = client.chat.completions.create(
                model      = settings.RAG_CONFIG.get('GROQ_MODEL', 'llama-3.1-8b-instant'),
                messages   = [{'role': 'user', 'content': prompt}],
                max_tokens = 600,
                temperature= 0.4,
            )
            text = (resp.choices[0].message.content or '').strip()
            if text:
                return text
        except Exception as e:
            logger.warning('Groq interpretation failed: %s', e)

    # ── Fallback: Gemini ──────────────────────────────────────────────────────
    gemini_key = getattr(settings, 'GEMINI_API_KEY', '')
    if gemini_key:
        try:
            from google import genai
            from google.genai import types
            client   = genai.Client(api_key=gemini_key)
            model_id = settings.RAG_CONFIG.get('GEMINI_MODEL', 'gemini-2.5-flash')
            config_kwargs = dict(temperature=0.4, max_output_tokens=1024)
            # Disable thinking for gemini-2.5-* to avoid burning token budget on reasoning
            if 'gemini-2.5' in model_id:
                config_kwargs['thinking_config'] = types.ThinkingConfig(thinking_budget=0)
            response = client.models.generate_content(
                model    = model_id,
                contents = [prompt],
                config   = types.GenerateContentConfig(**config_kwargs),
            )
            text = (response.text or '').strip()
            if text:
                return text
        except Exception as e:
            logger.warning('Gemini interpretation failed: %s', e)

    return _static_interpretation(result)


def _static_interpretation(result: dict) -> str:
    risk = result.get('risk_score')
    label = result.get('label', '')
    if risk is None:
        return f"Prediction complete: {label}. Please consult your doctor to understand what this means for your health."
    if risk >= 0.75:
        return (
            f"Your result shows {label}. This score suggests elevated risk. "
            "We recommend speaking with your doctor soon to discuss these findings. "
            "Early attention to risk factors can make a significant difference. "
            "Remember: this is an AI estimate, not a medical diagnosis."
        )
    if risk >= 0.45:
        return (
            f"Your result shows {label}. Your risk is in a moderate range. "
            "Maintaining a healthy lifestyle — regular exercise, balanced diet, and routine check-ups — "
            "can help reduce this risk. Discuss these results with your doctor at your next visit."
        )
    return (
        f"Your result shows {label}. Your risk appears to be in a low range. "
        "Continue maintaining healthy habits and regular medical check-ups. "
        "This result is an estimate based on the provided data — always consult your doctor for a full assessment."
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_feature_array(schema: dict, input_data: dict) -> np.ndarray:
    values = []
    for key in schema.keys():
        val = input_data.get(key, 0)
        try:
            values.append(float(str(val).replace(',', '.')))
        except (TypeError, ValueError):
            values.append(0.0)
    return np.array([values])


def _interpret(prediction, output_schema: dict) -> str:
    labels = output_schema.get('labels', {})
    if labels:
        key = str(int(round(prediction))) if isinstance(prediction, float) else str(prediction)
        if key in labels:
            return labels[key]
    if isinstance(prediction, float) and 0 <= prediction <= 1:
        if prediction >= 0.75:
            return f'High risk ({prediction:.0%})'
        elif prediction >= 0.45:
            return f'Moderate risk ({prediction:.0%})'
        else:
            return f'Low risk ({prediction:.0%})'
    return str(prediction)


def _serialize(val):
    if isinstance(val, np.integer):
        return int(val)
    if isinstance(val, np.floating):
        return float(val)
    if isinstance(val, np.ndarray):
        return val.tolist()
    return val


def _rule_based_fallback(ai_model, input_data: dict, input_file=None) -> dict:
    """Demo fallback when no model file is uploaded."""
    category = ai_model.input_type
    score = 0.0
    input_summary = ''

    if ai_model.input_type in ('image',):
        # For image input without a model: return placeholder
        score = 0.35
        input_summary = 'Image received (demo mode)'
    elif ai_model.input_type in ('eeg_csv', 'parquet', 'file') and input_file:
        try:
            import pandas as pd
            file_bytes = input_file.read()
            input_file.seek(0)
            df = pd.read_csv(io.BytesIO(file_bytes))
            rows, cols = df.shape
            input_summary = f'{rows} rows × {cols} columns'
            numeric_means = df.select_dtypes(include=[np.number]).mean().mean()
            score = min(0.9, max(0.05, float(numeric_means) / 100)) if not np.isnan(numeric_means) else 0.3
        except Exception:
            score = 0.3
    else:
        # Tabular fallback
        cat = ai_model.category
        if cat == 'diabetes':
            glucose = float(input_data.get('glucose_mmol', input_data.get('glucose', 5.5)) or 5.5)
            bmi     = float(input_data.get('bmi', 25) or 25)
            age     = float(input_data.get('age', 40) or 40)
            score = min(1.0, (max(0, glucose-5.5)/10)*0.5 + (max(0, bmi-25)/20)*0.3 + (max(0, age-40)/40)*0.2)
        elif cat == 'cardiovascular':
            sbp  = float(input_data.get('systolic_bp', 120) or 120)
            chol = float(input_data.get('cholesterol', 5.0) or 5.0)
            age  = float(input_data.get('age', 40) or 40)
            smok = float(input_data.get('smoker', 0) or 0)
            score = min(1.0, (max(0, sbp-120)/80)*0.3 + (max(0, chol-5)/5)*0.3 + (max(0, age-40)/40)*0.2 + smok*0.2)
        else:
            vals = []
            for v in input_data.values():
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
            score = min(0.5, sum(vals)/(len(vals)*100)) if vals else 0.3

    result = {
        'success': True,
        'prediction': score,
        'risk_score': score,
        'label': _interpret(score, ai_model.output_schema),
        'demo': True,
    }
    if input_summary:
        result['input_summary'] = input_summary
    return result

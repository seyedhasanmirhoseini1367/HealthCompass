"""
Patient-facing interpretation of AI model predictions.
Moved here from runner.py so handlers and views can both import it
without pulling in any model-loading code.
"""
import io
import logging

import numpy as np

logger = logging.getLogger(__name__)


def generate_interpretation(ai_model, result: dict, input_data: dict, user=None) -> str:
    """
    Generate a patient-friendly explanation.  Tries Groq → Gemini → static.

    The prompt carries this patient's model inputs and risk result, so it is
    patient-specific health data. When external processing is not permitted the
    built-in static interpretation is returned instead — the prediction itself
    still works, only the generated wording is withheld.

    `user` is optional for backward compatibility; callers that know the patient
    pass it so the consent check can run before the prompt is built.
    """
    from django.conf import settings
    from apps.accounts.egress import ExternalProcessingGuard

    if not ExternalProcessingGuard.allows(user, 'insights.interpretation'):
        return _static_interpretation(result)

    guide = ai_model.interpretation_guide.strip()
    if not guide:
        guide = (f"Model: {ai_model.name}. "
                 f"Category: {ai_model.get_category_display()}. "
                 f"{ai_model.description}")

    label    = result.get('label', 'unknown')
    risk     = result.get('risk_score')
    risk_pct = f'{risk * 100:.0f}%' if risk is not None else 'N/A'
    demo     = result.get('demo', False)

    inputs_str = ', '.join(
        f'{k}={v}' for k, v in input_data.items()
        if k != 'csrfmiddlewaretoken'
    ) if input_data else ''

    raw_summary  = result.get('input_summary', '')
    file_summary = (
        ', '.join(f'{k}: {v}' for k, v in raw_summary.items())
        if isinstance(raw_summary, dict)
        else str(raw_summary or '')
    )

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

    groq_key = getattr(settings, 'GROQ_API_KEY', '')
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key,
                          timeout=int(settings.RAG_CONFIG.get('PROVIDER_TIMEOUT', 45)))
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

    gemini_key = getattr(settings, 'GEMINI_API_KEY', '')
    if gemini_key:
        try:
            from google import genai
            from google.genai import types
            _ms = int(settings.RAG_CONFIG.get('PROVIDER_TIMEOUT', 45)) * 1000
            try:
                client = genai.Client(api_key=gemini_key, http_options={'timeout': _ms})
            except TypeError:
                client = genai.Client(api_key=gemini_key)
            model_id = settings.RAG_CONFIG.get('GEMINI_MODEL', 'gemini-2.5-flash')
            config_kwargs = dict(temperature=0.4, max_output_tokens=1024)
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


#: Prefixed to every interpretation of a rule-based result.
#
# The LLM prompt already carries a demo note, but this deterministic path is the
# one used whenever external processing is not permitted or the provider fails —
# and it said nothing. A patient who declined external processing was told
# "This score suggests elevated risk. We recommend speaking with your doctor
# soon" about a number produced by an invented weighted sum.
_DEMO_PREFIX = (
    'DEMONSTRATION RESULT — this model has no trained model file, so the score '
    'below comes from a simple rule-based placeholder, not from a validated '
    'medical model. It is not a risk assessment and must not be used to make '
    'any health decision. '
)


def _static_interpretation(result: dict) -> str:
    risk  = result.get('risk_score')
    label = result.get('label', '')
    prefix = _DEMO_PREFIX if result.get('demo') else ''

    if risk is None:
        return prefix + (
            f'Prediction complete: {label}. '
            'Please consult your doctor to understand what this means for your health.')
    if prefix:
        # Deliberately no risk-band language for a fabricated score: describing
        # an invented number as "elevated risk" is the misrepresentation this
        # prefix exists to prevent.
        return prefix + (
            f'The placeholder produced: {label}. '
            'Ask your doctor about any health concern — this output carries no '
            'clinical meaning.')
    if risk >= 0.75:
        return (
            f'Your result shows {label}. This score suggests elevated risk. '
            'We recommend speaking with your doctor soon to discuss these findings. '
            'Early attention to risk factors can make a significant difference. '
            'Remember: this is an AI estimate, not a medical diagnosis.'
        )
    if risk >= 0.45:
        return (
            f'Your result shows {label}. Your risk is in a moderate range. '
            'Maintaining a healthy lifestyle — regular exercise, balanced diet, and routine check-ups — '
            'can help reduce this risk. Discuss these results with your doctor at your next visit.'
        )
    return (
        f'Your result shows {label}. Your risk appears to be in a low range. '
        'Continue maintaining healthy habits and regular medical check-ups. '
        'This result is an estimate based on the provided data — '
        'always consult your doctor for a full assessment.'
    )


def _interpret(prediction, output_schema: dict) -> str:
    labels = output_schema.get('labels', {})
    if labels:
        key = str(int(round(prediction))) if isinstance(prediction, float) else str(prediction)
        if key in labels:
            return labels[key]
    if isinstance(prediction, float) and 0 <= prediction <= 1:
        if prediction >= 0.75:
            return f'High risk ({prediction:.0%})'
        if prediction >= 0.45:
            return f'Moderate risk ({prediction:.0%})'
        return f'Low risk ({prediction:.0%})'
    return str(prediction)


def _rule_based_demo_result(ai_model, input_data: dict, input_file=None) -> dict:
    """Return a plausible demo result for models that have no uploaded model file."""
    from .base import StandardPrediction
    import pandas as pd

    score         = 0.0
    input_summary = ''

    if ai_model.input_type == 'image':
        score         = 0.35
        input_summary = 'Image received (demo mode)'

    elif ai_model.input_type in ('eeg_csv', 'parquet', 'file') and input_file:
        try:
            file_bytes = input_file.read()
            input_file.seek(0)
            df         = pd.read_csv(io.BytesIO(file_bytes))
            rows, cols = df.shape
            input_summary  = f'{rows} rows × {cols} columns'
            numeric_means  = df.select_dtypes(include=[np.number]).mean().mean()
            score = (
                min(0.9, max(0.05, float(numeric_means) / 100))
                if not np.isnan(numeric_means) else 0.3
            )
        except Exception:
            score = 0.3

    else:
        cat = ai_model.category
        if cat == 'diabetes':
            glucose = float(input_data.get('glucose_mmol', input_data.get('glucose', 5.5)) or 5.5)
            bmi     = float(input_data.get('bmi', 25) or 25)
            age     = float(input_data.get('age', 40) or 40)
            score   = min(1.0,
                          (max(0, glucose - 5.5) / 10) * 0.5 +
                          (max(0, bmi    - 25)   / 20) * 0.3 +
                          (max(0, age    - 40)   / 40) * 0.2)
        elif cat == 'cardiovascular':
            sbp  = float(input_data.get('systolic_bp',  120) or 120)
            chol = float(input_data.get('cholesterol',  5.0) or 5.0)
            age  = float(input_data.get('age',           40) or 40)
            smok = float(input_data.get('smoker',         0) or 0)
            score = min(1.0,
                        (max(0, sbp  - 120) / 80) * 0.3 +
                        (max(0, chol - 5)   /  5) * 0.3 +
                        (max(0, age  - 40)  / 40) * 0.2 +
                        smok * 0.2)
        else:
            vals = []
            for v in input_data.values():
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
            score = min(0.5, sum(vals) / (len(vals) * 100)) if vals else 0.3

    d: dict = {
        'prediction':       score,
        'prediction_proba': score,
        'label':            _interpret(score, ai_model.output_schema),
        'demo':             True,
    }
    if input_summary:
        d['input_summary'] = input_summary
    return StandardPrediction.from_handler_dict(d).to_result_dict()

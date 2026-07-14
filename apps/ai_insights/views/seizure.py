import json
import logging

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from ..models import AIModel, ModelPrediction

logger = logging.getLogger(__name__)


def seizure_analysis(request):
    """Proxy parquet EEG file to PersonalPortfolio seizure comparison API."""
    if request.method == 'GET':
        return render(request, 'ai_insights/seizure_analysis.html', {})

    import requests as http_requests
    signal_file = request.FILES.get('signal_file')
    if not signal_file:
        return JsonResponse({'success': False, 'error': 'No file uploaded.'}, status=400)

    try:
        resp = http_requests.post(
            'https://hasanai.net/seizure-comparison/predict/',
            files={'signal_file': (signal_file.name, signal_file.read(), signal_file.content_type)},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        data['ai_interpretation'] = _generate_seizure_interpretation(data)

        if request.user.is_authenticated:
            try:
                admin_user = (
                    get_user_model().objects.filter(is_staff=True).first() or request.user
                )
                ai_model, _ = AIModel.objects.get_or_create(
                    slug='eeg-seizure-detection',
                    defaults={
                        'name':        'EEG Seizure Detection',
                        'description': 'Ensemble seizure detection via hasanai.net external API.',
                        'category':    AIModel.Category.NEUROLOGY,
                        'input_type':  AIModel.InputType.PARQUET,
                        'status':      AIModel.Status.ACTIVE,
                        'data_scientist': admin_user,
                    },
                )
                label = data.get('ensemble_label', '')
                confidence = data.get('ensemble_confidence') or data.get('confidence')
                if confidence is not None:
                    try:
                        confidence = float(confidence)
                    except (TypeError, ValueError):
                        confidence = None
                risk_score = None
                if confidence is not None:
                    risk_score = confidence if 'seizure' in label.lower() else (1 - confidence)
                ModelPrediction.objects.create(
                    model=ai_model,
                    patient=request.user,
                    input_data={'filename': signal_file.name},
                    result=data,
                    risk_score=risk_score,
                    interpretation=data.get('ai_interpretation', ''),
                )
                AIModel.objects.filter(pk=ai_model.pk).update(run_count=ai_model.run_count + 1)
            except Exception as save_err:
                logger.warning('Could not save seizure prediction: %s', save_err)

        return JsonResponse(data)
    except http_requests.Timeout:
        return JsonResponse({'success': False, 'error': 'The analysis timed out (>120 s). The EEG file may be too large, or the server is busy — please try again in a moment.'}, status=504)
    except http_requests.HTTPError as exc:
        http_status = exc.response.status_code if exc.response is not None else 500
        if http_status == 500:
            msg = 'The hasanai.net analysis server encountered an internal error. This is a temporary issue — please try again in a few minutes.'
        elif http_status == 503:
            msg = 'The analysis server is temporarily unavailable. Please try again shortly.'
        else:
            msg = f'The analysis server returned an unexpected error (HTTP {http_status}). Please try again.'
        logger.error('seizure_analysis HTTP %s from hasanai.net: %s', http_status, exc)
        return JsonResponse({'success': False, 'error': msg}, status=502)
    except Exception as exc:
        logger.exception('seizure_analysis proxy error: %s', exc)
        return JsonResponse({'success': False, 'error': 'Could not reach the analysis server. Please try again.'}, status=500)


def seizure_realtime(request):
    return render(request, 'ai_insights/seizure_realtime.html', {})


@csrf_exempt
def seizure_realtime_load(request):
    """Parse uploaded parquet/csv and return raw signal as JSON."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    import io
    uploaded_files = request.FILES.getlist('files')
    if not uploaded_files:
        return JsonResponse({'error': 'No files uploaded.'}, status=400)
    try:
        import pandas as pd
        frames, file_meta, ref_cols = [], [], None
        for f in uploaded_files:
            name = f.name.lower()
            raw = f.read()
            df_i = (pd.read_parquet(io.BytesIO(raw)) if name.endswith('.parquet')
                    else pd.read_csv(io.StringIO(raw.decode('utf-8', errors='replace'))))
            if 'EKG' in df_i.columns:
                df_i = df_i.drop(columns=['EKG'])
            cols = list(df_i.columns[:19])
            df_i = df_i[cols]
            if ref_cols is None:
                ref_cols = cols
            elif cols != ref_cols:
                return JsonResponse({'error': f'Column mismatch in "{f.name}".'}, status=400)
            file_meta.append({'name': f.name, 'samples': len(df_i)})
            frames.append(df_i)

        df = pd.concat(frames, ignore_index=True)
        total = len(df)
        if total < 256:
            return JsonResponse({'error': f'Signal too short ({total} samples).'}, status=400)

        data = {col: df[col].astype('float32').round(6).tolist() for col in ref_cols}
        return JsonResponse({
            'columns': ref_cols, 'total_samples': total,
            'sampling_rate': 200, 'duration_sec': round(total / 200, 1),
            'file_count': len(frames), 'files': file_meta, 'data': data,
        })
    except Exception as exc:
        logger.exception('seizure_realtime_load error: %s', exc)
        return JsonResponse({'error': str(exc)}, status=500)


def seizure_realtime_models(request):
    """Return the three locally available model variants + ensemble."""
    return JsonResponse({'models': [
        {'variant': 'ensemble',        'title': 'Ensemble (all 3 models)'},
        {'variant': 'cnn_transformer', 'title': 'CNN-Transformer'},
        {'variant': 'gru_attention',   'title': 'LSTM + Attention'},
        {'variant': 'fusion',          'title': 'CNN-LSTM Fusion'},
    ]})


@csrf_exempt
def seizure_realtime_predict_chunk(request):
    """Run local ONNX inference on a 10-second EEG window."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    import json as _json
    try:
        body = _json.loads(request.body)
    except _json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)

    data_dict = body.get('data')
    variant   = body.get('model_variant', 'ensemble')
    if not data_dict or not isinstance(data_dict, dict):
        return JsonResponse({'error': 'Missing "data" field.'}, status=400)

    try:
        from ..inference.seizure_inference import predict
        result = predict(data_dict, variant=variant)
        return JsonResponse(result)
    except Exception as exc:
        logger.exception('seizure_realtime_predict_chunk error: %s', exc)
        return JsonResponse({'error': str(exc)}, status=500)


def _generate_seizure_interpretation(data: dict) -> str:
    """Generate a clinical AI interpretation of the ensemble EEG result."""
    from django.conf import settings

    ensemble_label = data.get('ensemble_label', 'Unknown')
    votes          = data.get('ensemble_votes', {})
    results        = data.get('results', [])

    model_lines = []
    for r in results:
        if r.get('success'):
            conf = r.get('confidence')
            conf_str = f"{conf*100:.1f}%" if conf is not None else 'N/A'
            model_lines.append(f"  - {r.get('project_title', r.get('project_id', '?'))}: "
                                f"{r.get('prediction_label', '?')} ({conf_str})")
    models_summary = '\n'.join(model_lines) or '  (no individual model data)'
    votes_str = ', '.join(f'{lbl}: {cnt} vote(s)' for lbl, cnt in votes.items())

    prompt = f"""You are a clinical AI assistant helping a neurologist interpret an EEG ensemble analysis result.

ENSEMBLE RESULT:
- Final ensemble verdict: {ensemble_label}
- Votes: {votes_str}
- Individual model results:
{models_summary}

Write a concise 4-6 sentence clinical interpretation. Include:
1. What the ensemble verdict means clinically (LPD = Lateralised Periodic Discharge, Seizure = ictal activity)
2. What the voting pattern and confidence levels indicate about certainty
3. Key clinical implications and urgency
4. A clear reminder that automated EEG analysis must always be verified by a human expert

Write in plain paragraphs, no markdown, no bullet points."""

    gemini_key = getattr(settings, 'GEMINI_API_KEY', '')
    if gemini_key:
        try:
            from google import genai
            from google.genai import types
            client   = genai.Client(api_key=gemini_key)
            model_id = settings.RAG_CONFIG.get('GEMINI_MODEL', 'gemini-1.5-flash')
            cfg_kwargs = dict(temperature=0.3, max_output_tokens=600)
            if 'gemini-2.5' in model_id:
                cfg_kwargs['thinking_config'] = types.ThinkingConfig(thinking_budget=0)
            response = client.models.generate_content(
                model=model_id, contents=[prompt],
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            text = (response.text or '').strip()
            if text:
                return text
        except Exception as e:
            logger.warning('Gemini seizure interpretation failed: %s', e)

    anthropic_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if anthropic_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            msg = client.messages.create(
                model=settings.RAG_CONFIG.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001'),
                max_tokens=600,
                messages=[{'role': 'user', 'content': prompt}],
            )
            text = msg.content[0].text.strip()
            if text:
                return text
        except Exception as e:
            logger.warning('Anthropic seizure interpretation failed: %s', e)

    if 'seizure' in ensemble_label.lower():
        return (
            f"The ensemble voted in favor of {ensemble_label}. "
            "This outcome indicates probable ictal activity, which requires urgent medical evaluation. "
            "Immediate clinical assessment is recommended to confirm the finding and initiate appropriate management. "
            "A critical clinical caveat is that an automated EEG prediction should always be verified by a human expert before making treatment decisions."
        )
    return (
        f"The ensemble voted in favor of {ensemble_label}. "
        "This pattern may indicate interictal epileptiform activity requiring clinical correlation. "
        "The unanimous agreement across models increases confidence in the result, though individual variations in confidence levels should be considered. "
        "A critical clinical caveat is that an automated EEG prediction should always be verified by a human expert before making treatment decisions."
    )

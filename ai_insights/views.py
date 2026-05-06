import json
import logging
from collections import defaultdict
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import AIModel, ModelPrediction
from .forms import SubmitModelForm
from .runner import run_model, generate_interpretation

logger = logging.getLogger(__name__)


# ─── Public model catalog ─────────────────────────────────────────────────────

def model_list(request):
    models      = AIModel.objects.filter(status='active').order_by('-run_count', '-created_at')
    categories  = AIModel.Category.choices
    input_types = AIModel.InputType.choices
    return render(request, 'ai_insights/list.html', {
        'models':      models,
        'categories':  categories,
        'input_types': input_types,
        'total':       models.count(),
    })


def model_detail(request, slug):
    model = get_object_or_404(AIModel, slug=slug, status='active')
    user_predictions = []
    if request.user.is_authenticated:
        user_predictions = ModelPrediction.objects.filter(
            model=model, patient=request.user
        ).order_by('-created_at')[:5]
    return render(request, 'ai_insights/model_detail.html', {
        'model': model,
        'user_predictions': user_predictions,
        'input_fields': model.input_schema,
    })


# ─── Run prediction ───────────────────────────────────────────────────────────

def _sanitize(obj):
    """Recursively convert numpy / non-JSON-serializable types to Python primitives."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


@login_required
def run_prediction(request, slug):
    model = get_object_or_404(AIModel, slug=slug, status='active')

    if request.method != 'POST':
        return redirect('ai_insights:model_detail', slug=slug)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    try:
        # Collect tabular input
        input_data = {key: request.POST.get(key, '')
                      for key in model.input_schema.keys()}

        input_file = request.FILES.get('input_file') or None

        # Auto-detect handler slug from model file extension if not set
        handler_slug = model.handler_slug or ''
        if not handler_slug and model.model_file:
            ext = model.model_file.name.rsplit('.', 1)[-1].lower()
            if ext in ('pt', 'pth'):
                input_type_to_handler = {
                    'eeg_csv': 'seizure_eeg',
                    'image':   'image_classifier',
                    'parquet': 'tabular_passthrough',
                }
                handler_slug = input_type_to_handler.get(model.input_type, '')

        logger.info('run_prediction: model=%s handler_slug=%r (resolved=%r)',
                    model.slug, model.handler_slug, handler_slug)

        if handler_slug:
            from ai_insights.inference import get_handler
            model.handler_slug = handler_slug
            handler = get_handler(model)
            result = handler.run(
                uploaded_file=input_file,
                input_data=input_data if not input_file else None,
            )
        else:
            result = run_model(model, input_data, input_file)

        result = _sanitize(result)

        if not result.get('success'):
            raise ValueError(result.get('error', 'Prediction failed'))

        risk_score     = result.get('risk_score')
        interpretation = generate_interpretation(model, result, input_data)

        prediction = ModelPrediction.objects.create(
            model=model,
            patient=request.user,
            input_data=_sanitize(input_data),
            input_file=input_file,
            result=result,
            risk_score=risk_score,
            interpretation=interpretation,
        )

        AIModel.objects.filter(pk=model.pk).update(run_count=model.run_count + 1)

        from notifications.models import Notification
        Notification.objects.create(
            user=request.user,
            type=Notification.Type.MODEL_RESULT,
            title=f'AI result: {model.name}',
            message=f'Result: {result.get("label", "completed")}',
            link=f'/insights/prediction/{prediction.pk}/',
        )

        if risk_score and risk_score >= 0.75:
            from ai_insights.models import HealthAlert
            HealthAlert.objects.create(
                patient=request.user,
                severity=HealthAlert.Severity.WARNING,
                title=f'High risk detected: {model.name}',
                message=f'The model predicted {result.get("label")}. Consider consulting your doctor.',
            )

        if is_ajax:
            return JsonResponse({
                'success':       True,
                'result':        result,
                'interpretation': interpretation,
                'prediction_id': str(prediction.pk),
            })

        messages.success(request, f'Prediction complete: {result.get("label")}')
        return redirect('ai_insights:prediction_detail', pk=prediction.pk)

    except Exception as exc:
        err_msg = str(exc)
        logger.exception('run_prediction error for model %s: %s', slug, err_msg)
        if is_ajax:
            return JsonResponse({'success': False, 'error': err_msg})
        messages.error(request, f'Prediction failed: {err_msg}')
        return redirect('ai_insights:model_detail', slug=slug)


# ─── Prediction detail ────────────────────────────────────────────────────────

@login_required
def prediction_detail(request, pk):
    pred = get_object_or_404(ModelPrediction, pk=pk, patient=request.user)
    risk_pct = None
    if pred.risk_score is not None:
        risk_pct = round(pred.risk_score * 100, 1)
    return render(request, 'ai_insights/prediction_detail.html', {
        'pred': pred,
        'risk_pct': risk_pct,
    })


# ─── My predictions history ───────────────────────────────────────────────────

@login_required
def my_predictions(request):
    preds = ModelPrediction.objects.filter(patient=request.user).order_by('-created_at')
    return render(request, 'ai_insights/my_predictions.html', {'predictions': preds})


# ─── Data scientist: submit model ─────────────────────────────────────────────

@login_required
def submit_model(request):
    if not request.user.is_data_scientist:
        messages.error(request, 'Only data scientists can submit models.')
        return redirect('ai_insights:list')

    form = SubmitModelForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        model = form.save(commit=False)
        model.data_scientist = request.user
        model.save()
        messages.success(request, f'Model "{model.name}" submitted for review. An admin will review it shortly.')
        return redirect('ai_insights:my_models')

    return render(request, 'ai_insights/submit_model.html', {'form': form})


# ─── Data scientist: my models ────────────────────────────────────────────────

def debug_handlers(request):
    from django.http import HttpResponse
    from ai_insights.inference import list_handlers
    models = AIModel.objects.values('slug', 'handler_slug', 'input_type')
    lines = ['=== Registered handlers ===']
    lines += list_handlers()
    lines += ['', '=== Models in DB ===']
    for m in models:
        lines.append(f"slug={m['slug']}  handler_slug={m['handler_slug']!r}  input_type={m['input_type']}")
    return HttpResponse('\n'.join(lines), content_type='text/plain')


@login_required
def my_models(request):
    if not request.user.is_data_scientist:
        return redirect('ai_insights:list')
    models = AIModel.objects.filter(data_scientist=request.user).order_by('-created_at')
    return render(request, 'ai_insights/my_models.html', {'models': models})


# ─── Patient Analytics ────────────────────────────────────────────────────────

# ─── Seizure detection proxy ──────────────────────────────────────────────────

@login_required
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
        return JsonResponse(data)
    except http_requests.Timeout:
        return JsonResponse({'success': False, 'error': 'The analysis timed out (>120 s). The EEG file may be too large, or the server is busy — please try again in a moment.'}, status=504)
    except http_requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 500
        if status == 500:
            msg = 'The hasanai.net analysis server encountered an internal error. This is a temporary issue — please try again in a few minutes.'
        elif status == 503:
            msg = 'The analysis server is temporarily unavailable. Please try again shortly.'
        else:
            msg = f'The analysis server returned an unexpected error (HTTP {status}). Please try again.'
        logger.error('seizure_analysis HTTP %s from hasanai.net: %s', status, exc)
        return JsonResponse({'success': False, 'error': msg}, status=502)
    except Exception as exc:
        logger.exception('seizure_analysis proxy error: %s', exc)
        return JsonResponse({'success': False, 'error': 'Could not reach the analysis server. Please try again.'}, status=500)


def _generate_seizure_interpretation(data: dict) -> str:
    """Generate a clinical AI interpretation of the ensemble EEG result."""
    from django.conf import settings

    ensemble_label = data.get('ensemble_label', 'Unknown')
    votes          = data.get('ensemble_votes', {})
    results        = data.get('results', [])

    # Summarise per-model confidences
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

    # Try Gemini
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

    # Try Anthropic
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

    # Static fallback
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


@login_required
def patient_analytics(request):
    """
    Personal health analytics for patients — lives under /insights/analytics/
    so it sits naturally in the AI & Analytics section.
    """
    if not request.user.is_patient and not request.user.is_staff:
        messages.error(request, 'Access denied.')
        return redirect('dashboard:home')

    from medical_records.models import MedicalRecord, ParsedLabValue
    from ai_insights.models import HealthAlert

    patient = request.user

    # ── Lab value history ─────────────────────────────────────────────────────
    lab_qs = (
        ParsedLabValue.objects
        .filter(record__patient=patient, record__record_date__isnull=False)
        .select_related('record')
        .order_by('record__record_date')
    )

    biomarker_map = defaultdict(list)
    for lv in lab_qs:
        try:
            numeric = float(lv.value)
        except (ValueError, TypeError):
            continue
        biomarker_map[lv.parameter_name].append({
            'date':     str(lv.record.record_date),
            'value':    numeric,
            'unit':     lv.unit or '',
            'abnormal': lv.is_abnormal,
            'critical': lv.is_critical,
            'ref':      lv.reference_range or '',
        })

    trending_biomarkers = {k: v for k, v in biomarker_map.items() if len(v) >= 2}
    latest_values = {name: pts[-1] for name, pts in biomarker_map.items()}

    # ── Records by type ───────────────────────────────────────────────────────
    records_by_type = list(
        MedicalRecord.objects.filter(patient=patient)
        .values('record_type').annotate(count=Count('id')).order_by('-count')
    )

    # ── Monthly upload activity ───────────────────────────────────────────────
    monthly_uploads = list(
        MedicalRecord.objects.filter(patient=patient)
        .annotate(month=TruncMonth('uploaded_at'))
        .values('month').annotate(count=Count('id'))
        .order_by('month')
    )
    upload_labels = [r['month'].strftime('%b %Y') for r in monthly_uploads]
    upload_counts = [r['count'] for r in monthly_uploads]

    # ── Alerts ────────────────────────────────────────────────────────────────
    alerts_summary = {
        'critical': HealthAlert.objects.filter(patient=patient, severity='critical').count(),
        'warning':  HealthAlert.objects.filter(patient=patient, severity='warning').count(),
        'info':     HealthAlert.objects.filter(patient=patient, severity='info').count(),
    }
    recent_alerts = HealthAlert.objects.filter(patient=patient).order_by('-created_at')[:8]

    # ── AI risk predictions (field is 'model', not 'ai_model') ───────────────
    predictions = list(
        ModelPrediction.objects
        .filter(patient=patient, risk_score__isnull=False)
        .order_by('created_at')
        .values('created_at', 'risk_score', 'model__name')
    )
    pred_labels = [p['created_at'].strftime('%b %d') for p in predictions]
    pred_scores = [round(float(p['risk_score']) * 100, 1) for p in predictions]
    latest_risk  = pred_scores[-1] if pred_scores else None

    # ── Summary stats ─────────────────────────────────────────────────────────
    total_records    = MedicalRecord.objects.filter(patient=patient).count()
    total_biomarkers = len(biomarker_map)
    unread_alerts    = HealthAlert.objects.filter(patient=patient, is_read=False).count()
    last_record_date = (
        MedicalRecord.objects.filter(patient=patient, record_date__isnull=False)
        .order_by('-record_date').values_list('record_date', flat=True).first()
    )

    ctx = {
        'trending_json':       json.dumps(trending_biomarkers),
        'latest_values':       latest_values,
        'records_type_json':   json.dumps(records_by_type),
        'upload_labels_json':  json.dumps(upload_labels),
        'upload_counts_json':  json.dumps(upload_counts),
        'alerts_summary_json': json.dumps(alerts_summary),
        'pred_labels_json':    json.dumps(pred_labels),
        'pred_scores_json':    json.dumps(pred_scores),
        'recent_alerts':       recent_alerts,
        'latest_risk':         latest_risk,
        'total_records':       total_records,
        'total_biomarkers':    total_biomarkers,
        'unread_alerts':       unread_alerts,
        'last_record_date':    last_record_date,
        'has_data':            total_records > 0,
    }
    return render(request, 'ai_insights/patient_analytics.html', ctx)

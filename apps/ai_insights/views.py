import json
import logging
from collections import defaultdict
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.contrib.auth import get_user_model
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

from .models import AIModel, ModelPrediction, HealthAlert
from .forms import SubmitModelForm
from .runner import run_model, generate_interpretation

logger = logging.getLogger(__name__)


# ─── Public model catalog + analytics hub ────────────────────────────────────

def model_list(request):
    """Landing page — 3 navigation cards only."""
    total_models = AIModel.objects.filter(status='active').count()
    return render(request, 'ai_insights/list.html', {'total_models': total_models})


# ─── My Health analytics ──────────────────────────────────────────────────────

def _build_pop_biomarker_data(biomarker_names=None):
    """
    Returns (pop_trending, pop_latest, pop_avg, pop_unit) computed
    from all patients' ParsedLabValue rows.

    Groups by (parameter_name, unit) to avoid mixing incompatible units
    (e.g. µmol/L vs mg/dL for Creatinine). For each biomarker name the
    unit group with the most data points wins.

    pop_trending : {name: [{date, value, count, unit}, ...]}  monthly averages
    pop_latest   : {name: {value, unit, count}}               latest month avg
    pop_avg      : {name: float}                              overall avg (>=3 pts)
    pop_unit     : {name: str}                                canonical unit
    """
    from apps.medical_records.models import ParsedLabValue

    qs = ParsedLabValue.objects.select_related('record').values(
        'parameter_name', 'value', 'unit',
        'record__record_date', 'record__uploaded_at',
    )
    if biomarker_names:
        qs = qs.filter(parameter_name__in=biomarker_names)

    # key: (name, unit) → {month_key: [values]}
    per_unit_monthly = defaultdict(lambda: defaultdict(list))

    for lv in qs:
        try:
            numeric = float(lv['value'])
        except (ValueError, TypeError):
            continue
        date_val  = lv['record__record_date'] or lv['record__uploaded_at'].date()
        month_key = date_val.strftime('%Y-%m')
        key       = (lv['parameter_name'], lv.get('unit') or '')
        per_unit_monthly[key][month_key].append(numeric)

    # For each biomarker name pick the unit group with the most total data points
    by_name = defaultdict(list)
    for (name, unit), months in per_unit_monthly.items():
        total = sum(len(vs) for vs in months.values())
        by_name[name].append((unit, total, months))

    pop_trending, pop_latest, pop_avg, pop_unit = {}, {}, {}, {}
    for name, groups in by_name.items():
        best_unit, _, best_months = max(groups, key=lambda x: x[1])
        all_vals      = [v for vs in best_months.values() for v in vs]
        sorted_months = sorted(best_months.keys())
        series = [
            {'date': m, 'value': round(sum(best_months[m]) / len(best_months[m]), 2),
             'count': len(best_months[m]), 'unit': best_unit}
            for m in sorted_months if best_months[m]
        ]
        pop_unit[name] = best_unit
        if len(series) >= 2:
            pop_trending[name] = series
        if series:
            last = series[-1]
            pop_latest[name] = {'value': last['value'], 'unit': best_unit, 'count': last['count']}
        if len(all_vals) >= 3:
            pop_avg[name] = round(sum(all_vals) / len(all_vals), 2)

    return pop_trending, pop_latest, pop_avg, pop_unit


@login_required
def health_view(request):
    from apps.medical_records.models import MedicalRecord, ParsedLabValue

    patient = request.user

    lab_qs = (
        ParsedLabValue.objects
        .filter(record__patient=patient)
        .select_related('record')
        .order_by('record__record_date', 'record__uploaded_at')
    )
    biomarker_map = defaultdict(list)
    for lv in lab_qs:
        try:
            numeric = float(lv.value)
        except (ValueError, TypeError):
            continue
        date_val = lv.record.record_date or lv.record.uploaded_at.date()
        biomarker_map[lv.parameter_name].append({
            'date':     str(date_val),
            'value':    numeric,
            'unit':     lv.unit or '',
            'abnormal': lv.is_abnormal,
            'critical': lv.is_critical,
            'ref':      lv.reference_range or '',
        })

    trending_biomarkers = {k: v for k, v in biomarker_map.items() if len(v) >= 2}
    latest_values       = {name: pts[-1] for name, pts in biomarker_map.items()}

    # Population trend + average — only shown when units match personal data
    pop_trending_raw, _, pop_avg_raw, pop_unit = _build_pop_biomarker_data(
        biomarker_names=list(biomarker_map.keys()) or None
    )
    user_unit = {name: pts[-1]['unit'] for name, pts in biomarker_map.items() if pts}
    pop_avg = {
        name: val for name, val in pop_avg_raw.items()
        if pop_unit.get(name, '') == user_unit.get(name, '')
    }
    pop_trending_personal = {
        name: series for name, series in pop_trending_raw.items()
        if pop_unit.get(name, '') == user_unit.get(name, '')
    }

    records_by_type = list(
        MedicalRecord.objects.filter(patient=patient)
        .values('record_type').annotate(count=Count('id')).order_by('-count')
    )
    monthly_uploads = list(
        MedicalRecord.objects.filter(patient=patient)
        .annotate(month=TruncMonth('uploaded_at'))
        .values('month').annotate(count=Count('id')).order_by('month')
    )
    upload_labels = [r['month'].strftime('%b %Y') for r in monthly_uploads]
    upload_counts = [r['count']                   for r in monthly_uploads]

    alerts_summary = {
        'critical': HealthAlert.objects.filter(patient=patient, severity='critical').count(),
        'warning':  HealthAlert.objects.filter(patient=patient, severity='warning').count(),
        'info':     HealthAlert.objects.filter(patient=patient, severity='info').count(),
    }

    predictions = list(
        ModelPrediction.objects
        .filter(patient=patient, risk_score__isnull=False)
        .order_by('created_at')
        .values('created_at', 'risk_score')
    )
    pred_labels = [p['created_at'].strftime('%b %d') for p in predictions]
    pred_scores = [round(float(p['risk_score']) * 100, 1) for p in predictions]
    latest_risk = pred_scores[-1] if pred_scores else None

    total_records    = MedicalRecord.objects.filter(patient=patient).count()
    total_biomarkers = len(biomarker_map)
    unread_alerts    = HealthAlert.objects.filter(patient=patient, is_read=False).count()
    last_record_date = (
        MedicalRecord.objects.filter(patient=patient, record_date__isnull=False)
        .order_by('-record_date').values_list('record_date', flat=True).first()
    )

    return render(request, 'ai_insights/health.html', {
        'trending_json':          json.dumps(trending_biomarkers),
        'pop_avg_json':           json.dumps(pop_avg),
        'pop_trending_json':      json.dumps(pop_trending_personal),
        'latest_values':       latest_values,
        'records_type_json':   json.dumps(records_by_type),
        'upload_labels_json':  json.dumps(upload_labels),
        'upload_counts_json':  json.dumps(upload_counts),
        'alerts_summary_json': json.dumps(alerts_summary),
        'pred_labels_json':    json.dumps(pred_labels),
        'pred_scores_json':    json.dumps(pred_scores),
        'latest_risk':         latest_risk,
        'total_records':       total_records,
        'total_biomarkers':    total_biomarkers,
        'unread_alerts':       unread_alerts,
        'last_record_date':    last_record_date,
        'has_data':            total_records > 0,
    })


# ─── Population insights ──────────────────────────────────────────────────────

@login_required
def population_view(request):
    from apps.medical_records.models import MedicalRecord

    User = get_user_model()

    # Biomarker data aggregated across all patients
    pop_trending, pop_latest, _, _ = _build_pop_biomarker_data()

    total_patients   = User.objects.filter(is_active=True).count()
    total_biomarkers = len(pop_latest)

    # Alerts across all patients
    alerts_summary = {
        'critical': HealthAlert.objects.filter(severity='critical').count(),
        'warning':  HealthAlert.objects.filter(severity='warning').count(),
        'info':     HealthAlert.objects.filter(severity='info').count(),
    }
    total_alerts = sum(alerts_summary.values())

    # Records by type across all patients
    records_by_type = list(
        MedicalRecord.objects.values('record_type')
        .annotate(count=Count('id')).order_by('-count')
    )
    total_records = MedicalRecord.objects.count()

    # Upload activity across all patients (monthly)
    monthly_uploads = list(
        MedicalRecord.objects.annotate(month=TruncMonth('uploaded_at'))
        .values('month').annotate(count=Count('id')).order_by('month')
    )
    upload_labels = [r['month'].strftime('%b %Y') for r in monthly_uploads]
    upload_counts = [r['count']                   for r in monthly_uploads]

    # Risk score distribution across all patients
    all_scores = list(
        ModelPrediction.objects.filter(risk_score__isnull=False)
        .values_list('risk_score', flat=True)
    )
    risk_buckets = {'Low (0–30%)': 0, 'Moderate (30–70%)': 0, 'High (70–100%)': 0}
    for rs in all_scores:
        s = float(rs) * 100
        if s < 30:   risk_buckets['Low (0–30%)']      += 1
        elif s < 70: risk_buckets['Moderate (30–70%)'] += 1
        else:        risk_buckets['High (70–100%)']    += 1
    pop_avg_risk = round(sum(float(r)*100 for r in all_scores) / len(all_scores), 1) if all_scores else None

    return render(request, 'ai_insights/population.html', {
        'trending_json':       json.dumps(pop_trending),
        'latest_values':       pop_latest,
        'alerts_summary_json': json.dumps(alerts_summary),
        'records_type_json':   json.dumps(records_by_type),
        'upload_labels_json':  json.dumps(upload_labels),
        'upload_counts_json':  json.dumps(upload_counts),
        'risk_labels_json':    json.dumps(list(risk_buckets.keys())),
        'risk_counts_json':    json.dumps(list(risk_buckets.values())),
        'pop_avg_risk':        pop_avg_risk,
        'total_patients':      total_patients,
        'total_biomarkers':    total_biomarkers,
        'total_alerts':        total_alerts,
        'total_records':       total_records,
        'has_data':            total_records > 0,
    })


# ─── AI Models catalog ────────────────────────────────────────────────────────

def models_view(request):
    ai_models  = AIModel.objects.filter(status='active').order_by('-run_count', '-created_at')
    categories = AIModel.Category.choices
    return render(request, 'ai_insights/models.html', {
        'models':     ai_models,
        'categories': categories,
        'total':      ai_models.count(),
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
            from apps.ai_insights.inference import get_handler
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

        from apps.notifications.models import Notification
        Notification.objects.create(
            user=request.user,
            type=Notification.Type.MODEL_RESULT,
            title=f'AI result: {model.name}',
            message=f'Result: {result.get("label", "completed")}',
            link=f'/insights/prediction/{prediction.pk}/',
        )

        if risk_score and risk_score >= 0.75:
            from apps.ai_insights.models import HealthAlert
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
    from apps.ai_insights.inference import list_handlers
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

        # Persist result only for authenticated users
        if request.user.is_authenticated:
            try:
                admin_user = (
                    get_user_model().objects.filter(is_staff=True).first() or request.user
                )
                ai_model, _ = AIModel.objects.get_or_create(
                    slug='eeg-seizure-detection',
                    defaults={
                        'name': 'EEG Seizure Detection',
                        'description': 'Ensemble seizure detection via hasanai.net external API.',
                        'category': AIModel.Category.NEUROLOGY,
                        'input_type': AIModel.InputType.PARQUET,
                        'status': AIModel.Status.ACTIVE,
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


# ─── Real-time EEG inference (local, no proxy) ───────────────────────────────

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
        {'variant': 'ensemble',       'title': 'Ensemble (all 3 models)'},
        {'variant': 'cnn_transformer','title': 'CNN-Transformer'},
        {'variant': 'gru_attention',  'title': 'LSTM + Attention'},
        {'variant': 'fusion',         'title': 'CNN-LSTM Fusion'},
    ]})


@csrf_exempt
def seizure_realtime_predict_chunk(request):
    """Run local PyTorch inference on a 10-second EEG window."""
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
        from apps.ai_insights.inference.seizure_inference import predict
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
def ajax_lab_records(request):
    """Return user's records that have parsed lab values — for the 'use from records' feature."""
    from apps.medical_records.models import MedicalRecord, ParsedLabValue
    ids_with_labs = (
        ParsedLabValue.objects
        .filter(record__patient=request.user)
        .values_list('record_id', flat=True)
        .distinct()
    )
    qs = (MedicalRecord.objects
          .filter(patient=request.user, id__in=ids_with_labs)
          .order_by('-record_date', '-uploaded_at')
          .values('id', 'title', 'record_type', 'record_date'))
    records = [
        {
            'id': str(r['id']),
            'title': r['title'],
            'record_type': r['record_type'],
            'record_date': str(r['record_date']) if r['record_date'] else None,
        }
        for r in qs
    ]
    return JsonResponse({'records': records})


@login_required
def ajax_record_labs(request, pk):
    """Return lab values for a specific record, for pre-filling AI model input fields."""
    from apps.medical_records.models import MedicalRecord, ParsedLabValue
    try:
        record = MedicalRecord.objects.get(pk=pk, patient=request.user)
    except MedicalRecord.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    lab_values = list(
        ParsedLabValue.objects
        .filter(record=record)
        .values('parameter_name', 'value', 'unit')
    )
    return JsonResponse({'record_id': str(pk), 'title': record.title, 'lab_values': lab_values})


@login_required
def patient_analytics(request):
    """
    Personal health analytics for patients — lives under /insights/analytics/
    so it sits naturally in the AI & Analytics section.
    """
    if not request.user.is_patient and not request.user.is_staff:
        messages.error(request, 'Access denied.')
        return redirect('dashboard:home')

    from apps.medical_records.models import MedicalRecord, ParsedLabValue
    from apps.ai_insights.models import HealthAlert

    patient = request.user

    # ── Lab value history ─────────────────────────────────────────────────────
    lab_qs = (
        ParsedLabValue.objects
        .filter(record__patient=patient)
        .select_related('record')
        .order_by('record__record_date', 'record__uploaded_at')
    )

    biomarker_map = defaultdict(list)
    for lv in lab_qs:
        try:
            numeric = float(lv.value)
        except (ValueError, TypeError):
            continue
        date_val = lv.record.record_date or lv.record.uploaded_at.date()
        biomarker_map[lv.parameter_name].append({
            'date':     str(date_val),
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

    # ── Population Insights (anonymised aggregates) ───────────────────────────
    from apps.rag_assistant.models import QueryLog, ChatSession, GeneralKnowledgeChunk
    from django.utils import timezone
    import datetime

    User = get_user_model()

    # Query mode distribution
    pop_mode_dist = list(
        QueryLog.objects.values('query_mode')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    # Monthly query volume (last 6 months)
    six_months_ago = timezone.now() - datetime.timedelta(days=182)
    monthly_queries = list(
        QueryLog.objects.filter(created_at__gte=six_months_ago)
        .annotate(month=TruncMonth('created_at'))
        .values('month').annotate(count=Count('id'))
        .order_by('month')
    )
    pop_query_labels = [r['month'].strftime('%b %Y') for r in monthly_queries]
    pop_query_counts = [r['count'] for r in monthly_queries]

    # Knowledge base topic distribution
    pop_topics = list(
        GeneralKnowledgeChunk.objects.exclude(topic='')
        .values('topic').annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    # Safety interventions per month (last 6 months)
    monthly_safety = list(
        QueryLog.objects.filter(created_at__gte=six_months_ago, safety_routed=True)
        .annotate(month=TruncMonth('created_at'))
        .values('month').annotate(count=Count('id'))
        .order_by('month')
    )
    pop_safety_labels = [r['month'].strftime('%b %Y') for r in monthly_safety]
    pop_safety_counts = [r['count'] for r in monthly_safety]

    # System-wide summary stats
    pop_total_users    = User.objects.filter(is_active=True).count()
    pop_total_queries  = QueryLog.objects.count()
    pop_total_sessions = ChatSession.objects.count()
    pop_safety_total   = QueryLog.objects.filter(safety_routed=True).count()
    pop_kb_chunks      = GeneralKnowledgeChunk.objects.count()
    pop_safety_pct     = round(pop_safety_total / max(pop_total_queries, 1) * 100, 1)

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
        # population
        'pop_mode_dist_json':    json.dumps(pop_mode_dist),
        'pop_query_labels_json': json.dumps(pop_query_labels),
        'pop_query_counts_json': json.dumps(pop_query_counts),
        'pop_topics_json':       json.dumps(pop_topics),
        'pop_safety_labels_json':json.dumps(pop_safety_labels),
        'pop_safety_counts_json':json.dumps(pop_safety_counts),
        'pop_total_users':       pop_total_users,
        'pop_total_queries':     pop_total_queries,
        'pop_total_sessions':    pop_total_sessions,
        'pop_kb_chunks':         pop_kb_chunks,
        'pop_safety_pct':        pop_safety_pct,
        'pop_safety_total':      pop_safety_total,
    }
    return render(request, 'ai_insights/patient_analytics.html', ctx)


# ── ICU Demo (merged from icu_dashboard app) ──────────────────────────────────
import os
import random as _random

import pandas as _pd

_DATA_BASE  = getattr(__import__('django.conf', fromlist=['settings']).settings, 'ICU_DEMO_DATA_PATH', '')
_INDEX_PATH = os.path.join(_DATA_BASE, 'index.csv') if _DATA_BASE else ''

_VITAL_ITEMIDS = {
    'hr':   [220045],
    'map':  [220052, 220181],
    'spo2': [220277],
    'rr':   [220210],
}
_LAB_ITEMIDS = {
    'creatinine': [50912, 220615],
    'lactate':    [50813, 225668],
    'wbc':        [51301, 220546],
    'platelets':  [51265, 227457],
}
_SNAPSHOT_ITEMS = {
    'Creatinine':  ([50912, 220615],  (0.6,  1.2,  'mg/dL')),
    'WBC':         ([51301, 220546],  (4.5,  11.0, 'K/uL')),
    'Platelets':   ([51265, 227457],  (150,  400,  'K/uL')),
    'Hemoglobin':  ([51222],          (12,   17,   'g/dL')),
    'Lactate':     ([50813, 225668],  (0.5,  2.0,  'mmol/L')),
    'Glucose':     ([50931, 226537],  (70,   110,  'mg/dL')),
    'BUN':         ([51006, 225624],  (7,    20,   'mg/dL')),
    'Potassium':   ([50971, 227442],  (3.5,  5.0,  'mEq/L')),
    'Sodium':      ([50983, 220645],  (136,  145,  'mEq/L')),
    'Bicarbonate': ([50882, 227443],  (22,   29,   'mEq/L')),
    'INR':         ([51237],          (0.8,  1.2,  '')),
    'Bilirubin':   ([50885],          (0.2,  1.2,  'mg/dL')),
}


def _icu_last(df, itemids):
    sub = df[df['itemid'].isin(itemids)].dropna(subset=['value'])
    if sub.empty:
        return None
    try:
        return float(sub.sort_values('delta_hours').iloc[-1]['value'])
    except Exception:
        return None


def _icu_series(df, itemids, max_pts=60):
    sub = (df[df['itemid'].isin(itemids) & (df['delta_hours'] >= 0)]
           .dropna(subset=['value'])
           .sort_values('delta_hours'))
    if sub.empty:
        return []
    if len(sub) > max_pts:
        idx = [int(i * (len(sub) - 1) / (max_pts - 1)) for i in range(max_pts)]
        sub = sub.iloc[idx]
    return [[round(float(r['delta_hours']), 2), round(float(r['value']), 2)]
            for _, r in sub.iterrows()]


def _icu_compute_sofa(df):
    crea  = _icu_last(df, [50912, 220615])
    plt   = _icu_last(df, [51265, 227457])
    bili  = _icu_last(df, [50885])
    map_  = _icu_last(df, [220052, 220181])
    pao2  = _icu_last(df, [50821])
    fio2  = _icu_last(df, [223835])
    spo2  = _icu_last(df, [220277])
    gcs_e = _icu_last(df, [220739])
    gcs_v = _icu_last(df, [223900])
    gcs_m = _icu_last(df, [223901])

    resp = None
    if pao2 and fio2 and fio2 > 0:
        pf = pao2 / (fio2 / 100 if fio2 > 1 else fio2)
        resp = 4 if pf < 100 else 3 if pf < 200 else 2 if pf < 300 else 1 if pf < 400 else 0
    elif spo2:
        resp = 2 if spo2 < 90 else 1 if spo2 < 95 else 0

    coag  = (4 if plt < 20 else 3 if plt < 50 else 2 if plt < 100 else 1 if plt < 150 else 0) if plt else None
    liver = (4 if bili >= 12 else 3 if bili >= 6 else 2 if bili >= 2 else 1 if bili >= 1.2 else 0) if bili else None
    cardio = (1 if map_ < 70 else 0) if map_ else None

    neuro = None
    if gcs_e is not None and gcs_v is not None and gcs_m is not None:
        gcs = int(gcs_e + gcs_v + gcs_m)
        neuro = 4 if gcs < 6 else 3 if gcs < 9 else 2 if gcs < 12 else 1 if gcs < 14 else 0

    renal = (4 if crea >= 5 else 3 if crea >= 3.5 else 2 if crea >= 2 else 1 if crea >= 1.2 else 0) if crea else None

    scores = {
        'Respiratory': resp, 'Coagulation': coag,
        'Liver': liver,      'Cardiovascular': cardio,
        'Neurological': neuro, 'Renal': renal,
    }
    total = sum(v for v in scores.values() if v is not None)
    return scores, total


def _icu_sofa_color(s):
    if s is None:
        return '#475569'
    return ('#22c55e', '#84cc16', '#f59e0b', '#f97316', '#ef4444')[min(s, 4)]


def _icu_events(df, n=15):
    icu = df[df['delta_hours'] >= 0].sort_values('delta_hours', ascending=False).head(n)
    out = []
    for _, r in icu.iterrows():
        try:
            val = f"{float(r['value']):.1f}"
        except Exception:
            val = '—'
        unit = str(r['unit'] or '').strip()
        out.append({'delta': f"T+{r['delta_hours']:.1f}h", 'source': str(r['source']),
                    'label': str(r['label']), 'value': f"{val} {unit}".strip()})
    return out


def _icu_lab_snapshot(df):
    rows = []
    for name, (ids, (lo, hi, unit)) in _SNAPSHOT_ITEMS.items():
        val = _icu_last(df, ids)
        if val is None:
            continue
        flag = 'danger' if (val < lo * 0.7 or val > hi * 1.5) else 'warning' if (val < lo or val > hi) else ''
        rows.append({'name': name, 'value': round(val, 2), 'unit': unit,
                     'range': f"{lo}–{hi}", 'flag': flag})
    return rows


def _icu_unit_abbr(full):
    s = str(full)
    return s.split('(')[-1].replace(')', '').strip() if '(' in s else s[:12]


def _icu_mock_eeg(seed=7):
    _random.seed(seed)
    labels = [f'{i}:00' for i in range(24)]
    prob = [round(_random.uniform(0.01, 0.15), 3) for _ in range(18)]
    prob += [round(_random.uniform(0.55, 0.82), 3) for _ in range(3)]
    prob += [round(_random.uniform(0.05, 0.20), 3) for _ in range(3)]
    return labels, prob


def _icu_mock_context():
    _random.seed(42)
    vitals = {
        'hr':   [[i * 0.5, round(92 + _random.uniform(-8, 8), 1)] for i in range(60)],
        'map':  [[i * 0.5, round(63 + _random.uniform(-8, 6), 1)] for i in range(60)],
        'spo2': [[i * 0.5, round(91 + _random.uniform(-3, 5), 1)] for i in range(60)],
        'rr':   [[i * 0.5, round(22 + _random.uniform(-3, 5), 1)] for i in range(60)],
    }
    labs = {
        'creatinine': [[i * 4, round(2.1 + i * 0.12 + _random.uniform(-0.1, 0.1), 2)] for i in range(8)],
        'lactate':    [[i * 4, round(2.8 - i * 0.05 + _random.uniform(-0.1, 0.1), 2)] for i in range(8)],
        'wbc':        [[i * 4, round(13 + _random.uniform(-2, 3), 1)] for i in range(8)],
        'platelets':  [[i * 4, round(148 - i * 2 + _random.uniform(-5, 5), 0)] for i in range(8)],
    }
    sofa_raw = {'Respiratory': 3, 'Coagulation': 1, 'Liver': 1,
                'Cardiovascular': 2, 'Neurological': 4, 'Renal': 4}
    sofa_display = [{'name': k, 'score': v, 'color': _icu_sofa_color(v)} for k, v in sofa_raw.items()]
    patient = {
        'subject_id': 10016742, 'stay_id': 37057036, 'age': 67, 'gender': 'Male',
        'unit': 'Medical ICU (MICU)', 'unit_short': 'MICU',
        'intime': '2178-07-03 22:45', 'los_days': 3.25,
        'admission_type': 'EW EMER.', 'n_events': 1842,
        'hospital_expire_flag': 1, 'discharge_location': 'DIED',
    }
    events = [
        {'delta': 'T+27.5h', 'source': 'CHART', 'label': 'Heart Rate',     'value': '94 bpm'},
        {'delta': 'T+27.2h', 'source': 'CHART', 'label': 'MAP',            'value': '54 mmHg'},
        {'delta': 'T+26.0h', 'source': 'INPUT', 'label': 'Norepinephrine', 'value': '0.08 mcg/kg/min'},
        {'delta': 'T+24.0h', 'source': 'OUTPUT','label': 'Foley Urine',    'value': '28 mL'},
        {'delta': 'T+22.0h', 'source': 'LAB',   'label': 'Creatinine',     'value': '5.5 mg/dL'},
        {'delta': 'T+20.0h', 'source': 'LAB',   'label': 'Lactate',        'value': '4.2 mmol/L'},
        {'delta': 'T+18.0h', 'source': 'LAB',   'label': 'WBC',            'value': '13.6 K/uL'},
        {'delta': 'T+12.0h', 'source': 'INPUT', 'label': 'NS 500mL',       'value': '500 mL'},
        {'delta': 'T+6.0h',  'source': 'LAB',   'label': 'Platelet Count', 'value': '148 K/uL'},
    ]
    lab_snap = [
        {'name': 'Creatinine', 'value': 5.5,  'unit': 'mg/dL',  'range': '0.6–1.2', 'flag': 'danger'},
        {'name': 'Lactate',    'value': 4.2,  'unit': 'mmol/L', 'range': '0.5–2.0', 'flag': 'danger'},
        {'name': 'WBC',        'value': 13.6, 'unit': 'K/uL',   'range': '4.5–11',  'flag': 'warning'},
        {'name': 'Platelets',  'value': 148,  'unit': 'K/uL',   'range': '150–400', 'flag': 'warning'},
        {'name': 'Hemoglobin', 'value': 9.2,  'unit': 'g/dL',   'range': '12–17',   'flag': 'danger'},
        {'name': 'BUN',        'value': 107,  'unit': 'mg/dL',  'range': '7–20',    'flag': 'danger'},
        {'name': 'Sodium',     'value': 132,  'unit': 'mEq/L',  'range': '136–145', 'flag': 'warning'},
        {'name': 'Glucose',    'value': 188,  'unit': 'mg/dL',  'range': '70–110',  'flag': 'warning'},
    ]
    eeg_labels, eeg_prob = _icu_mock_eeg()
    return {
        'real_data': False, 'patients': [], 'selected_id': None, 'patient': patient,
        'vitals': {k: json.dumps(v) for k, v in vitals.items()},
        'labs':   {k: json.dumps(v) for k, v in labs.items()},
        'sofa_scores': sofa_display, 'sofa_total': sum(sofa_raw.values()),
        'lab_snap': lab_snap, 'events': events,
        'eeg_labels': json.dumps(eeg_labels), 'eeg_prob': json.dumps(eeg_prob),
    }


def icu_demo(request):
    if not (_DATA_BASE and os.path.isfile(_INDEX_PATH)):
        return render(request, 'icu_dashboard/demo.html', _icu_mock_context())

    index = _pd.read_csv(_INDEX_PATH)
    patients = []
    for _, row in index.iterrows():
        abbr = _icu_unit_abbr(str(row['first_careunit']))
        flag = int(row['hospital_expire_flag'])
        outcome = '☠ Died' if flag else '✓ Survived'
        patients.append({
            'stay_id': int(row['stay_id']),
            'label': (f"Subj {row['subject_id']} · {row['age']}"
                      f"{'M' if row['gender']=='M' else 'F'} · {abbr} · "
                      f"LOS {round(float(row['los']),1)}d · {outcome}"),
            'hospital_expire_flag': flag,
        })

    try:
        selected_id = int(request.GET.get('stay', patients[0]['stay_id']))
    except Exception:
        selected_id = patients[0]['stay_id']

    row = index[index['stay_id'] == selected_id].iloc[0]
    fname = os.path.join(_DATA_BASE, 'all_stays', str(row['file_path']))
    df = _pd.read_csv(fname)
    df['value'] = _pd.to_numeric(df['value'], errors='coerce')

    abbr = _icu_unit_abbr(str(row['first_careunit']))
    patient = {
        'subject_id': int(row['subject_id']), 'stay_id': selected_id,
        'age': int(row['age']), 'gender': 'Male' if row['gender'] == 'M' else 'Female',
        'unit': str(row['first_careunit']), 'unit_short': abbr,
        'intime': str(row['intime'])[:16], 'los_days': round(float(row['los']), 1),
        'admission_type': str(row['admission_type']), 'n_events': int(row['n_events']),
        'hospital_expire_flag': int(row['hospital_expire_flag']),
        'discharge_location': str(row['discharge_location']),
    }

    vitals = {k: _icu_series(df, v)             for k, v in _VITAL_ITEMIDS.items()}
    labs   = {k: _icu_series(df, v, max_pts=30) for k, v in _LAB_ITEMIDS.items()}
    sofa_raw, sofa_total = _icu_compute_sofa(df)
    sofa_display = [{'name': k, 'score': v, 'color': _icu_sofa_color(v)} for k, v in sofa_raw.items()]
    eeg_labels, eeg_prob = _icu_mock_eeg(seed=selected_id % 97)

    ctx = {
        'real_data': True, 'patients': patients, 'selected_id': selected_id,
        'patient': patient,
        'vitals': {k: json.dumps(v) for k, v in vitals.items()},
        'labs':   {k: json.dumps(v) for k, v in labs.items()},
        'sofa_scores': sofa_display, 'sofa_total': sofa_total,
        'lab_snap': _icu_lab_snapshot(df), 'events': _icu_events(df),
        'eeg_labels': json.dumps(eeg_labels), 'eeg_prob': json.dumps(eeg_prob),
    }
    return render(request, 'icu_dashboard/demo.html', ctx)
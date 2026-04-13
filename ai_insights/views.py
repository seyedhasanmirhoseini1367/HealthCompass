import json
import logging
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import AIModel, ModelPrediction
from .forms import SubmitModelForm
from .runner import run_model, generate_interpretation

logger = logging.getLogger(__name__)


# ─── Public model catalog ─────────────────────────────────────────────────────

def model_list(request):
    models = AIModel.objects.filter(status='active').order_by('-run_count', '-created_at')
    categories = AIModel.Category.choices
    active_cat = request.GET.get('cat', '')
    if active_cat:
        models = models.filter(category=active_cat)
    return render(request, 'ai_insights/list.html', {
        'models': models,
        'categories': categories,
        'active_cat': active_cat,
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

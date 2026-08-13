import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404

from ..models import AIModel, ModelPrediction
from ..forms import SubmitModelForm
from ..inference.interpretation import generate_interpretation

logger = logging.getLogger(__name__)


from ..services.utils import _sanitize
from ..inference.base import InferenceError
from healthcompass.errors import client_error
from django.db.models import F


def model_list(request):
    """Landing page — 3 navigation cards only."""
    total_models = AIModel.objects.filter(status='active').count()
    return render(request, 'ai_insights/list.html', {'total_models': total_models})


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


_RUNNABLE_STATUSES = frozenset({AIModel.Status.ACTIVE, AIModel.Status.APPROVED})


@login_required
def run_prediction(request, slug):
    # Fetch by slug only — status is enforced explicitly below.
    # Using a separate PermissionDenied (403) rather than a 404 means the caller
    # gets a meaningful error instead of a "page not found" when a model exists
    # but isn't runnable yet.
    model = get_object_or_404(AIModel, slug=slug)

    if request.method != 'POST':
        return redirect('ai_insights:model_detail', slug=slug)

    # ── Status gate — OUTSIDE try/except so PermissionDenied escapes as HTTP 403 ──
    if model.status not in _RUNNABLE_STATUSES:
        raise PermissionDenied

    # APPROVED = passed review but not yet public.
    # Only the owning data scientist and staff may test-run pre-release models.
    if (model.status == AIModel.Status.APPROVED and
            not (request.user == model.data_scientist or request.user.is_staff)):
        raise PermissionDenied

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    try:
        input_data = {key: request.POST.get(key, '')
                      for key in model.input_schema.keys()}

        input_file = request.FILES.get('input_file') or None

        # Handler resolution and dispatch live in inference.run_model, shared
        # with the mobile API. They were duplicated here, and the mobile copy
        # imported a module that no longer existed — two definitions of "how a
        # model runs" is how the web and mobile paths drift apart.
        from ..inference import run_model

        logger.info('run_prediction: model=%s handler_slug=%r',
                    model.slug, model.handler_slug)
        result = _sanitize(run_model(model, input_data, input_file))

        if not result.get('success'):
            raise ValueError(result.get('error', 'Prediction failed'))

        risk_score     = result.get('risk_score')
        interpretation = generate_interpretation(model, result, input_data, user=request.user)

        prediction = ModelPrediction.objects.create(
            model=model,
            patient=request.user,
            input_data=_sanitize(input_data),
            input_file=input_file,
            result=result,
            risk_score=risk_score,
            interpretation=interpretation,
        )

        AIModel.objects.filter(pk=model.pk).update(run_count=F('run_count') + 1)

        from apps.notifications.models import Notification
        Notification.objects.create(
            user=request.user,
            type=Notification.Type.MODEL_RESULT,
            title=f'AI result: {model.name}',
            message=f'Result: {result.get("label", "completed")}',
            link=f'/insights/prediction/{prediction.pk}/',
        )

        # A demo result must never raise a clinical alert.
        #
        # _rule_based_demo_result() computes an invented weighted sum for models
        # with no uploaded model file. That score can exceed 0.75, which used to
        # create a real HealthAlert titled "High risk detected" and a push
        # notification — a fabricated number presented to the patient as a
        # clinical finding. Alerts are for model output, not for placeholders.
        if risk_score and risk_score >= 0.75 and not result.get('demo'):
            from ..models import HealthAlert
            HealthAlert.objects.create(
                patient=request.user,
                severity=HealthAlert.Severity.WARNING,
                title=f'High risk detected: {model.name}',
                message=f'The model predicted {result.get("label")}. Consider consulting your doctor.',
            )

        if is_ajax:
            return JsonResponse({
                'success':        True,
                'result':         result,
                'interpretation': interpretation,
                'prediction_id':  str(prediction.pk),
            })

        messages.success(request, f'Prediction complete: {result.get("label")}')
        return redirect('ai_insights:prediction_detail', pk=prediction.pk)

    except InferenceError as exc:
        # InferenceError is the one exception type whose message is WRITTEN for
        # the patient — "missing: age, glucose", "expected CSV, you uploaded
        # .PDF". Named explicitly rather than inlining str(exc) so it is
        # distinguishable from an internal leak, both to a reader and to the
        # sweep in apps/accounts/test_error_disclosure.py.
        user_facing_message = str(exc)
        if is_ajax:
            return JsonResponse({'success': False, 'error': user_facing_message})
        messages.error(request, f'Prediction failed: {user_facing_message}')
        return redirect('ai_insights:model_detail', slug=slug)

    except Exception as exc:
        # Anything else is internal: admin instructions, provider errors and
        # stack detail were previously rendered straight into the page.
        payload = client_error(exc, context='run_prediction', log=logger)
        if is_ajax:
            return JsonResponse({'success': False, **payload})
        messages.error(request, f"{payload['error']} (ref {payload['reference']})")
        return redirect('ai_insights:model_detail', slug=slug)


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


@login_required
def my_predictions(request):
    preds = ModelPrediction.objects.filter(patient=request.user).order_by('-created_at')
    return render(request, 'ai_insights/my_predictions.html', {'predictions': preds})


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


@login_required
def debug_handlers(request):
    """
    Handler/model diagnostics. Staff only.

    This had no decorator at all: any anonymous visitor could enumerate every
    registered inference handler and every model's slug, handler_slug and
    input_type — including PENDING and REJECTED models that are not otherwise
    visible anywhere.
    """
    if not request.user.is_staff:
        raise PermissionDenied

    from ..inference import list_handlers
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

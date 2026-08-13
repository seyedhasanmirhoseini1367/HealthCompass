import json
import logging
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.http import JsonResponse
from django.shortcuts import render, redirect

from apps.accounts.safe_json import script_safe_json
from ..models import AIModel, ModelPrediction, HealthAlert
from ..services import (
    get_patient_biomarker_data,
    get_population_biomarker_stats,
    get_population_risk_buckets,
)

logger = logging.getLogger(__name__)


def _build_pop_biomarker_data(biomarker_names=None):
    """
    Thin wrapper kept for backward-compatibility with any code that may still
    call this helper directly.  Delegates to the service layer.
    """
    return get_population_biomarker_stats(biomarker_names=biomarker_names)


@login_required
def health_view(request):
    from apps.medical_records.models import MedicalRecord

    patient = request.user

    biomarker_map, trending_biomarkers, latest_values = get_patient_biomarker_data(patient)

    pop_trending_raw, _, pop_avg_raw, pop_unit = get_population_biomarker_stats(
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
        'trending_json':          script_safe_json(trending_biomarkers),
        'pop_avg_json':           script_safe_json(pop_avg),
        'pop_trending_json':      script_safe_json(pop_trending_personal),
        'latest_values':          latest_values,
        'records_type_json':      script_safe_json(records_by_type),
        'upload_labels_json':     script_safe_json(upload_labels),
        'upload_counts_json':     script_safe_json(upload_counts),
        'alerts_summary_json':    script_safe_json(alerts_summary),
        'pred_labels_json':       script_safe_json(pred_labels),
        'pred_scores_json':       script_safe_json(pred_scores),
        'latest_risk':            latest_risk,
        'total_records':          total_records,
        'total_biomarkers':       total_biomarkers,
        'unread_alerts':          unread_alerts,
        'last_record_date':       last_record_date,
        'has_data':               total_records > 0,
    })


@login_required
def population_view(request):
    # Cohort statistics, not patient-facing. This view was @login_required only,
    # so any patient could read biomarker averages, alert counts and risk
    # buckets over every other patient in the deployment.
    from apps.accounts.authz import can_view_population_analytics
    if not can_view_population_analytics(request.user):
        messages.error(request, 'Access denied.')
        return redirect('dashboard:home')

    from apps.medical_records.models import MedicalRecord

    User = get_user_model()

    pop_trending, pop_latest, _, _ = get_population_biomarker_stats()

    total_patients   = User.objects.filter(is_active=True).count()
    total_biomarkers = len(pop_latest)

    alerts_summary = {
        'critical': HealthAlert.objects.filter(severity='critical').count(),
        'warning':  HealthAlert.objects.filter(severity='warning').count(),
        'info':     HealthAlert.objects.filter(severity='info').count(),
    }
    total_alerts = sum(alerts_summary.values())

    records_by_type = list(
        MedicalRecord.objects.values('record_type')
        .annotate(count=Count('id')).order_by('-count')
    )
    total_records = MedicalRecord.objects.count()

    monthly_uploads = list(
        MedicalRecord.objects.annotate(month=TruncMonth('uploaded_at'))
        .values('month').annotate(count=Count('id')).order_by('month')
    )
    upload_labels = [r['month'].strftime('%b %Y') for r in monthly_uploads]
    upload_counts = [r['count']                   for r in monthly_uploads]

    risk_buckets, pop_avg_risk, _ = get_population_risk_buckets()

    return render(request, 'ai_insights/population.html', {
        'trending_json':       script_safe_json(pop_trending),
        'latest_values':       pop_latest,
        'alerts_summary_json': script_safe_json(alerts_summary),
        'records_type_json':   script_safe_json(records_by_type),
        'upload_labels_json':  script_safe_json(upload_labels),
        'upload_counts_json':  script_safe_json(upload_counts),
        'risk_labels_json':    script_safe_json(list(risk_buckets.keys())),
        'risk_counts_json':    script_safe_json(list(risk_buckets.values())),
        'pop_avg_risk':        pop_avg_risk,
        'total_patients':      total_patients,
        'total_biomarkers':    total_biomarkers,
        'total_alerts':        total_alerts,
        'total_records':       total_records,
        'has_data':            total_records > 0,
    })


@login_required
def patient_analytics(request):
    if not request.user.is_patient and not request.user.is_staff:
        messages.error(request, 'Access denied.')
        return redirect('dashboard:home')

    from apps.medical_records.models import MedicalRecord
    import datetime
    from django.utils import timezone

    patient = request.user

    biomarker_map, trending_biomarkers, latest_values = get_patient_biomarker_data(patient)

    records_by_type = list(
        MedicalRecord.objects.filter(patient=patient)
        .values('record_type').annotate(count=Count('id')).order_by('-count')
    )

    monthly_uploads = list(
        MedicalRecord.objects.filter(patient=patient)
        .annotate(month=TruncMonth('uploaded_at'))
        .values('month').annotate(count=Count('id'))
        .order_by('month')
    )
    upload_labels = [r['month'].strftime('%b %Y') for r in monthly_uploads]
    upload_counts = [r['count'] for r in monthly_uploads]

    alerts_summary = {
        'critical': HealthAlert.objects.filter(patient=patient, severity='critical').count(),
        'warning':  HealthAlert.objects.filter(patient=patient, severity='warning').count(),
        'info':     HealthAlert.objects.filter(patient=patient, severity='info').count(),
    }
    recent_alerts = HealthAlert.objects.filter(patient=patient).order_by('-created_at')[:8]

    predictions = list(
        ModelPrediction.objects
        .filter(patient=patient, risk_score__isnull=False)
        .order_by('created_at')
        .values('created_at', 'risk_score', 'model__name')
    )
    pred_labels = [p['created_at'].strftime('%b %d') for p in predictions]
    pred_scores = [round(float(p['risk_score']) * 100, 1) for p in predictions]
    latest_risk  = pred_scores[-1] if pred_scores else None

    total_records    = MedicalRecord.objects.filter(patient=patient).count()
    total_biomarkers = len(biomarker_map)
    unread_alerts    = HealthAlert.objects.filter(patient=patient, is_read=False).count()
    last_record_date = (
        MedicalRecord.objects.filter(patient=patient, record_date__isnull=False)
        .order_by('-record_date').values_list('record_date', flat=True).first()
    )

    from apps.rag_assistant.models import QueryLog, ChatSession, GeneralKnowledgeChunk
    User = get_user_model()

    pop_mode_dist = list(
        QueryLog.objects.values('query_mode')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    six_months_ago = timezone.now() - datetime.timedelta(days=182)
    monthly_queries = list(
        QueryLog.objects.filter(created_at__gte=six_months_ago)
        .annotate(month=TruncMonth('created_at'))
        .values('month').annotate(count=Count('id'))
        .order_by('month')
    )
    pop_query_labels = [r['month'].strftime('%b %Y') for r in monthly_queries]
    pop_query_counts = [r['count'] for r in monthly_queries]

    pop_topics = list(
        GeneralKnowledgeChunk.objects.exclude(topic='')
        .values('topic').annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    monthly_safety = list(
        QueryLog.objects.filter(created_at__gte=six_months_ago, safety_routed=True)
        .annotate(month=TruncMonth('created_at'))
        .values('month').annotate(count=Count('id'))
        .order_by('month')
    )
    pop_safety_labels = [r['month'].strftime('%b %Y') for r in monthly_safety]
    pop_safety_counts = [r['count'] for r in monthly_safety]

    pop_total_users    = User.objects.filter(is_active=True).count()
    pop_total_queries  = QueryLog.objects.count()
    pop_total_sessions = ChatSession.objects.count()
    pop_safety_total   = QueryLog.objects.filter(safety_routed=True).count()
    pop_kb_chunks      = GeneralKnowledgeChunk.objects.count()
    pop_safety_pct     = round(pop_safety_total / max(pop_total_queries, 1) * 100, 1)

    ctx = {
        'trending_json':         script_safe_json(trending_biomarkers),
        'latest_values':         latest_values,
        'records_type_json':     script_safe_json(records_by_type),
        'upload_labels_json':    script_safe_json(upload_labels),
        'upload_counts_json':    script_safe_json(upload_counts),
        'alerts_summary_json':   script_safe_json(alerts_summary),
        'pred_labels_json':      script_safe_json(pred_labels),
        'pred_scores_json':      script_safe_json(pred_scores),
        'recent_alerts':         recent_alerts,
        'latest_risk':           latest_risk,
        'total_records':         total_records,
        'total_biomarkers':      total_biomarkers,
        'unread_alerts':         unread_alerts,
        'last_record_date':      last_record_date,
        'has_data':              total_records > 0,
        'pop_mode_dist_json':    script_safe_json(pop_mode_dist),
        'pop_query_labels_json': script_safe_json(pop_query_labels),
        'pop_query_counts_json': script_safe_json(pop_query_counts),
        'pop_topics_json':       script_safe_json(pop_topics),
        'pop_safety_labels_json':script_safe_json(pop_safety_labels),
        'pop_safety_counts_json':script_safe_json(pop_safety_counts),
        'pop_total_users':       pop_total_users,
        'pop_total_queries':     pop_total_queries,
        'pop_total_sessions':    pop_total_sessions,
        'pop_kb_chunks':         pop_kb_chunks,
        'pop_safety_pct':        pop_safety_pct,
        'pop_safety_total':      pop_safety_total,
    }
    return render(request, 'ai_insights/patient_analytics.html', ctx)


@login_required
def ajax_lab_records(request):
    """Return user's records that have parsed lab values."""
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
            'id':          str(r['id']),
            'title':       r['title'],
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

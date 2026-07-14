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

from ..models import AIModel, ModelPrediction, HealthAlert

logger = logging.getLogger(__name__)


def _build_pop_biomarker_data(biomarker_names=None):
    """
    Returns (pop_trending, pop_latest, pop_avg, pop_unit) computed from all
    patients' ParsedLabValue rows.  Groups by (parameter_name, unit) to avoid
    mixing incompatible units.  Results cached 1 hour.
    """
    from django.core.cache import cache
    from apps.medical_records.models import ParsedLabValue

    _cache_key = (
        'ai_insights:pop_biomarker:'
        + (','.join(sorted(biomarker_names)) if biomarker_names else 'all')
    )
    cached = cache.get(_cache_key)
    if cached is not None:
        return cached

    qs = ParsedLabValue.objects.select_related('record').values(
        'parameter_name', 'value', 'unit',
        'record__record_date', 'record__uploaded_at',
    )
    if biomarker_names:
        qs = qs.filter(parameter_name__in=biomarker_names)

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

    result = pop_trending, pop_latest, pop_avg, pop_unit
    cache.set(_cache_key, result, 3600)
    return result


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

    from apps.medical_records.models import MedicalRecord
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

    from apps.medical_records.models import MedicalRecord
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
        'latest_values':          latest_values,
        'records_type_json':      json.dumps(records_by_type),
        'upload_labels_json':     json.dumps(upload_labels),
        'upload_counts_json':     json.dumps(upload_counts),
        'alerts_summary_json':    json.dumps(alerts_summary),
        'pred_labels_json':       json.dumps(pred_labels),
        'pred_scores_json':       json.dumps(pred_scores),
        'latest_risk':            latest_risk,
        'total_records':          total_records,
        'total_biomarkers':       total_biomarkers,
        'unread_alerts':          unread_alerts,
        'last_record_date':       last_record_date,
        'has_data':               total_records > 0,
    })


@login_required
def population_view(request):
    from apps.medical_records.models import MedicalRecord

    User = get_user_model()

    pop_trending, pop_latest, _, _ = _build_pop_biomarker_data()

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


@login_required
def patient_analytics(request):
    if not request.user.is_patient and not request.user.is_staff:
        messages.error(request, 'Access denied.')
        return redirect('dashboard:home')

    from apps.medical_records.models import MedicalRecord, ParsedLabValue
    import datetime
    from django.utils import timezone

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
    latest_values = {name: pts[-1] for name, pts in biomarker_map.items()}

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
        'trending_json':         json.dumps(trending_biomarkers),
        'latest_values':         latest_values,
        'records_type_json':     json.dumps(records_by_type),
        'upload_labels_json':    json.dumps(upload_labels),
        'upload_counts_json':    json.dumps(upload_counts),
        'alerts_summary_json':   json.dumps(alerts_summary),
        'pred_labels_json':      json.dumps(pred_labels),
        'pred_scores_json':      json.dumps(pred_scores),
        'recent_alerts':         recent_alerts,
        'latest_risk':           latest_risk,
        'total_records':         total_records,
        'total_biomarkers':      total_biomarkers,
        'unread_alerts':         unread_alerts,
        'last_record_date':      last_record_date,
        'has_data':              total_records > 0,
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

from collections import defaultdict

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.medical_records.models import MedicalRecord
from ..serializers import (UserSerializer, MedicalRecordSerializer,
                            HealthAlertSerializer, ModelPredictionSerializer)

_POPULATION_INSIGHTS_TTL = 3600


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_summary(request):
    from apps.ai_insights.models import ModelPrediction, HealthAlert

    user    = request.user
    records = MedicalRecord.objects.filter(patient=user)

    by_type = {}
    for rt, label in MedicalRecord.RecordType.choices:
        count = records.filter(record_type=rt).count()
        if count:
            by_type[label] = count

    recent_alerts = HealthAlert.objects.filter(
        patient=user, is_read=False
    ).order_by('-created_at')[:5]

    recent_predictions = ModelPrediction.objects.filter(
        patient=user
    ).order_by('-created_at')[:3]

    latest_pred = ModelPrediction.objects.filter(
        patient=user, risk_score__isnull=False
    ).order_by('-created_at').first()

    return Response({
        'total_records':      records.count(),
        'flagged_count':      records.filter(is_flagged=True).count(),
        'unread_alerts':      HealthAlert.objects.filter(patient=user, is_read=False).count(),
        'records_by_type':    by_type,
        'user':               UserSerializer(user).data,
        'recent_alerts':      HealthAlertSerializer(recent_alerts, many=True).data,
        'recent_predictions': ModelPredictionSerializer(recent_predictions, many=True).data,
        'latest_risk':        round(float(latest_pred.risk_score) * 100, 1) if latest_pred else None,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def analytics(request):
    from apps.ai_insights.models import ModelPrediction, HealthAlert
    from apps.medical_records.models import ParsedLabValue

    patient = request.user

    lab_qs = (ParsedLabValue.objects
              .filter(record__patient=patient)
              .select_related('record')
              .order_by('record__record_date', 'record__uploaded_at'))

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

    biomarker_latest = {name: pts[-1] for name, pts in biomarker_map.items()}
    biomarker_trends = {name: pts for name, pts in biomarker_map.items() if len(pts) >= 2}

    records = MedicalRecord.objects.filter(patient=patient)
    records_by_type = {}
    for rt, label in MedicalRecord.RecordType.choices:
        count = records.filter(record_type=rt).count()
        if count:
            records_by_type[label] = count

    alerts_qs      = HealthAlert.objects.filter(patient=patient).order_by('-created_at')[:8]
    predictions_qs = ModelPrediction.objects.filter(patient=patient).order_by('-created_at')[:5]

    latest_pred    = ModelPrediction.objects.filter(patient=patient, risk_score__isnull=False).order_by('-created_at').first()
    latest_risk    = round(float(latest_pred.risk_score) * 100, 1) if latest_pred else None
    last_record    = records.filter(record_date__isnull=False).order_by('-record_date').first()

    return Response({
        'total_records':    records.count(),
        'flagged_count':    records.filter(is_flagged=True).count(),
        'total_biomarkers': len(biomarker_map),
        'unread_alerts':    HealthAlert.objects.filter(patient=patient, is_read=False).count(),
        'latest_risk':      latest_risk,
        'last_record_date': str(last_record.record_date) if last_record else None,
        'biomarker_latest': biomarker_latest,
        'biomarker_trends': biomarker_trends,
        'alerts':           HealthAlertSerializer(alerts_qs, many=True).data,
        'predictions':      ModelPredictionSerializer(predictions_qs, many=True).data,
        'records_by_type':  records_by_type,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def population_insights(request):
    cached = cache.get('api:population_insights')
    if cached is not None:
        return Response(cached)

    from apps.medical_records.models import ParsedLabValue
    from apps.ai_insights.models import HealthAlert, ModelPrediction

    qs = ParsedLabValue.objects.select_related('record').values(
        'parameter_name', 'value', 'unit',
        'record__record_date', 'record__uploaded_at',
    )
    per_unit = defaultdict(lambda: defaultdict(list))
    for lv in qs:
        try:
            numeric = float(lv['value'])
        except (TypeError, ValueError):
            continue
        date_val  = lv['record__record_date'] or lv['record__uploaded_at'].date()
        month_key = str(date_val)[:7]
        key = (lv['parameter_name'], lv.get('unit') or '')
        per_unit[key][month_key].append(numeric)

    by_name = defaultdict(list)
    for (name, unit), months in per_unit.items():
        total = sum(len(v) for v in months.values())
        by_name[name].append((unit, total, months))

    pop_latest, pop_avg, pop_unit = {}, {}, {}
    for name, groups in by_name.items():
        best_unit, _, best_months = max(groups, key=lambda x: x[1])
        all_vals = [v for vs in best_months.values() for v in vs]
        sorted_months = sorted(best_months.keys())
        if sorted_months:
            last_vals = best_months[sorted_months[-1]]
            pop_latest[name] = {
                'value': round(sum(last_vals) / len(last_vals), 2),
                'unit':  best_unit,
                'count': len(all_vals),
            }
        if len(all_vals) >= 3:
            pop_avg[name] = round(sum(all_vals) / len(all_vals), 2)
        pop_unit[name] = best_unit

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

    pop_avg_risk = round(sum(float(r) * 100 for r in all_scores) / len(all_scores), 1) \
        if all_scores else None

    alerts_summary = {
        'critical': HealthAlert.objects.filter(severity='critical').count(),
        'warning':  HealthAlert.objects.filter(severity='warning').count(),
        'info':     HealthAlert.objects.filter(severity='info').count(),
    }

    records_by_type = {}
    for rt, label in MedicalRecord.RecordType.choices:
        count = MedicalRecord.objects.filter(record_type=rt).count()
        if count:
            records_by_type[label] = count

    User = get_user_model()

    payload = {
        'total_patients':    User.objects.filter(is_active=True, role='patient').count(),
        'total_biomarkers':  len(pop_latest),
        'total_predictions': len(all_scores),
        'pop_avg_risk':      pop_avg_risk,
        'pop_latest':        pop_latest,
        'pop_avg':           pop_avg,
        'pop_unit':          pop_unit,
        'risk_buckets':      risk_buckets,
        'alerts_summary':    alerts_summary,
        'records_by_type':   records_by_type,
    }
    cache.set('api:population_insights', payload, _POPULATION_INSIGHTS_TTL)
    return Response(payload)

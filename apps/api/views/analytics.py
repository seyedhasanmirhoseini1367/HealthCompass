from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.medical_records.models import MedicalRecord
from apps.ai_insights.services import (
    get_patient_biomarker_data,
    get_population_biomarker_stats,
    get_population_risk_buckets,
)
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

    patient = request.user

    biomarker_map, _, _ = get_patient_biomarker_data(patient)
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

    from apps.ai_insights.models import HealthAlert, ModelPrediction

    _, pop_latest, pop_avg, pop_unit = get_population_biomarker_stats()
    risk_buckets, pop_avg_risk, _ = get_population_risk_buckets()

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
        'total_predictions': sum(risk_buckets.values()),
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

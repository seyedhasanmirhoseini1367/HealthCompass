def get_patient_dashboard_data(patient):
    """Return all dashboard data for a patient.

    Called by both the web home view (patient branch) and the API
    dashboard_summary endpoint so both callers share the same query logic.

    Keys returned:
        recent_records, total_records, flagged_count,
        recent_alerts, unread_alerts,
        recent_predictions, total_predictions,
        latest_pred, records_by_type
    """
    from apps.medical_records.models import MedicalRecord
    from apps.ai_insights.models import HealthAlert, ModelPrediction

    records          = MedicalRecord.objects.filter(patient=patient)
    unread_alerts_qs = HealthAlert.objects.filter(patient=patient, is_read=False)

    by_type = {}
    for rt, label in MedicalRecord.RecordType.choices:
        count = records.filter(record_type=rt).count()
        if count:
            by_type[label] = count

    recent_alerts      = list(unread_alerts_qs.order_by('-created_at')[:5])
    recent_predictions = list(
        ModelPrediction.objects.filter(patient=patient).order_by('-created_at')[:3]
    )
    latest_pred = (
        ModelPrediction.objects
        .filter(patient=patient, risk_score__isnull=False)
        .order_by('-created_at')
        .first()
    )

    return {
        'recent_records':     list(records.order_by('-uploaded_at')[:5]),
        'total_records':      records.count(),
        'flagged_count':      records.filter(is_flagged=True).count(),
        'recent_alerts':      recent_alerts,
        'unread_alerts':      unread_alerts_qs.count(),
        'recent_predictions': recent_predictions,
        'total_predictions':  ModelPrediction.objects.filter(patient=patient).count(),
        'latest_pred':        latest_pred,
        'records_by_type':    by_type,
    }

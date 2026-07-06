import json
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, Avg
from django.db.models.functions import Length, TruncDate
from django.utils import timezone

from apps.medical_records.models import MedicalRecord
from apps.ai_insights.models import HealthAlert
from apps.notifications.models import Notification
from apps.accounts.models import CustomUser, PatientDoctorRelationship


@login_required
def home(request):
    user = request.user
    ctx = {}

    if user.is_patient:
        from apps.ai_insights.models import ModelPrediction
        ctx['records'] = MedicalRecord.objects.filter(patient=user).order_by('-uploaded_at')[:5]
        ctx['alerts'] = HealthAlert.objects.filter(patient=user, is_read=False)[:5]
        ctx['total_records'] = MedicalRecord.objects.filter(patient=user).count()
        ctx['unread_alerts'] = HealthAlert.objects.filter(patient=user, is_read=False).count()
        ctx['recent_predictions'] = ModelPrediction.objects.filter(patient=user).order_by('-created_at')[:3]
        ctx['total_predictions'] = ModelPrediction.objects.filter(patient=user).count()
        template = 'dashboard/patient.html'

    elif user.is_doctor:
        rels = PatientDoctorRelationship.objects.filter(doctor=user, is_active=True).select_related('patient')
        pids = list(rels.values_list('patient_id', flat=True))
        ctx['patient_count'] = len(pids)
        ctx['relationships'] = rels[:10]
        ctx['recent_records'] = MedicalRecord.objects.filter(patient_id__in=pids).order_by('-uploaded_at')[:6]
        ctx['unread_alerts'] = HealthAlert.objects.filter(patient_id__in=pids, is_read=False).count()
        template = 'dashboard/doctor.html'

    elif user.is_data_scientist:
        from apps.ai_insights.models import AIModel
        ctx['my_models'] = AIModel.objects.filter(data_scientist=user).order_by('-created_at')[:5]
        ctx['total_models'] = AIModel.objects.filter(data_scientist=user).count()
        ctx['total_runs'] = AIModel.objects.filter(data_scientist=user).aggregate(
            total=Sum('run_count')
        )['total'] or 0
        template = 'dashboard/scientist.html'

    elif user.is_hospital_admin:
        rels = PatientDoctorRelationship.objects.filter(
            linked_by=user).select_related('patient', 'doctor').order_by('-created_at')
        ctx['relationships'] = rels[:20]
        ctx['total_links'] = rels.count()
        ctx['all_patients'] = CustomUser.objects.filter(role='patient', is_active=True).order_by('username')
        ctx['all_doctors'] = CustomUser.objects.filter(role='doctor', is_active=True).select_related('doctor_profile').order_by('username')
        ctx['total_patients'] = ctx['all_patients'].count()
        ctx['total_doctors'] = ctx['all_doctors'].count()
        template = 'dashboard/hospital_admin.html'

    else:
        # Django admin users
        from apps.ai_insights.models import AIModel
        ctx['pending_models'] = AIModel.objects.filter(status='pending').count()
        ctx['total_users'] = CustomUser.objects.count()
        ctx['pending_scientists'] = CustomUser.objects.filter(role='data_scientist', is_approved=False).count()
        template = 'dashboard/admin.html'

    ctx['notifications'] = Notification.objects.filter(user=user, is_read=False)[:5]
    return render(request, template, ctx)


# ─── Doctor: view a patient's records ────────────────────────────────────────

@login_required
def patient_records(request, patient_pk):
    """Doctor views a linked patient's medical records."""
    if not request.user.is_doctor:
        messages.error(request, 'Access denied.')
        return redirect('dashboard:home')

    # Verify the doctor-patient relationship
    patient = get_object_or_404(CustomUser, pk=patient_pk, role='patient')
    relationship = get_object_or_404(
        PatientDoctorRelationship,
        doctor=request.user,
        patient=patient,
        is_active=True
    )

    records = MedicalRecord.objects.filter(patient=patient).order_by('-uploaded_at')
    alerts = HealthAlert.objects.filter(patient=patient).order_by('-created_at')[:5]

    return render(request, 'dashboard/patient_records.html', {
        'patient': patient,
        'records': records,
        'alerts': alerts,
        'relationship': relationship,
    })


@login_required
def doctor_record_detail(request, record_pk):
    """Doctor views a specific record of a linked patient."""
    if not request.user.is_doctor:
        return redirect('dashboard:home')

    record = get_object_or_404(MedicalRecord, pk=record_pk)

    # Ensure this doctor is linked to this patient
    get_object_or_404(
        PatientDoctorRelationship,
        doctor=request.user,
        patient=record.patient,
        is_active=True
    )

    lab_values = record.lab_values.all()
    return render(request, 'dashboard/doctor_record.html', {
        'record': record,
        'lab_values': lab_values,
    })


# ─── Hospital Admin: create / remove link ────────────────────────────────────

@login_required
def create_link(request):
    if not request.user.is_hospital_admin:
        return redirect('dashboard:home')

    if request.method == 'POST':
        patient_id = request.POST.get('patient_id')
        doctor_id = request.POST.get('doctor_id')

        try:
            patient = CustomUser.objects.get(pk=patient_id, role='patient')
            doctor = CustomUser.objects.get(pk=doctor_id, role='doctor')
        except CustomUser.DoesNotExist:
            messages.error(request, 'Invalid patient or doctor selected.')
            return redirect('dashboard:home')

        _, created = PatientDoctorRelationship.objects.get_or_create(
            patient=patient,
            doctor=doctor,
            defaults={'linked_by': request.user, 'is_active': True}
        )

        if created:
            # Notify both parties
            Notification.objects.create(
                user=patient,
                type=Notification.Type.SYSTEM,
                title='Doctor linked to your account',
                message=f'Dr. {doctor.get_full_name() or doctor.username} has been linked to your account.',
            )
            Notification.objects.create(
                user=doctor,
                type=Notification.Type.SYSTEM,
                title='New patient linked',
                message=f'{patient.get_full_name() or patient.username} has been linked to your account.',
            )
            messages.success(request, f'Successfully linked {patient.username} ↔ Dr. {doctor.username}.')
        else:
            messages.warning(request, 'This patient–doctor link already exists.')

    return redirect('dashboard:home')


@login_required
def remove_link(request, pk):
    if not request.user.is_hospital_admin:
        return redirect('dashboard:home')

    rel = get_object_or_404(PatientDoctorRelationship, pk=pk, linked_by=request.user)
    if request.method == 'POST':
        rel.delete()
        messages.success(request, 'Link removed.')
    return redirect('dashboard:home')


# ─── Monitoring Dashboard ─────────────────────────────────────────────────────

@login_required
def monitoring(request):
    """
    MLOps monitoring view — accessible to data scientists, hospital admins,
    and superusers. Shows RAG system health, AI inference stats, and platform
    overview derived entirely from existing DB models (no new migrations).
    """
    user = request.user
    allowed = user.is_data_scientist or user.is_hospital_admin or user.is_staff or user.is_superuser
    if not allowed:
        messages.error(request, 'Access denied.')
        return redirect('dashboard:home')

    now = timezone.now()
    ago_7d  = now - timedelta(days=7)
    ago_30d = now - timedelta(days=30)
    ago_14d = now - timedelta(days=14)

    # ── RAG System ────────────────────────────────────────────────────────────
    from apps.rag_assistant.models import QueryLog, MedicalChunk, MedicalDocument, ChatSession

    total_queries   = QueryLog.objects.count()
    queries_7d      = QueryLog.objects.filter(created_at__gte=ago_7d).count()
    queries_30d     = QueryLog.objects.filter(created_at__gte=ago_30d).count()
    total_sessions  = ChatSession.objects.count()

    avg_response_len = (
        QueryLog.objects.aggregate(avg=Avg(Length('response')))['avg'] or 0
    )

    # Retrieval failures = queries where sources JSON is an empty list
    retrieval_failures = QueryLog.objects.filter(sources=[]).count()
    retrieval_failure_pct = (
        round(retrieval_failures / total_queries * 100, 1) if total_queries else 0
    )

    # LLM provider breakdown
    provider_counts = list(
        QueryLog.objects.exclude(llm_provider='')
        .values('llm_provider').annotate(count=Count('id')).order_by('-count')
    )

    # Avg retrieved chunks per query
    avg_chunks = (
        QueryLog.objects
        .filter(retrieved_chunks_count__isnull=False)
        .aggregate(avg=Avg('retrieved_chunks_count'))['avg'] or 0
    )

    # Chunk / embedding coverage
    total_chunks    = MedicalChunk.objects.count()
    embedded_chunks = MedicalChunk.objects.exclude(embedding__isnull=True).exclude(embedding=b'').count()
    embedding_pct   = round(embedded_chunks / total_chunks * 100, 1) if total_chunks else 0

    total_documents = MedicalDocument.objects.count()
    docs_by_type = list(
        MedicalDocument.objects.values('document_type')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    # Daily query trend — last 14 days
    daily_qs = (
        QueryLog.objects
        .filter(created_at__gte=ago_14d)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(count=Count('id'))
        .order_by('day')
    )
    # Fill missing days with 0
    daily_map = {r['day'].isoformat(): r['count'] for r in daily_qs}
    chart_labels, chart_data = [], []
    for i in range(14):
        d = (ago_14d + timedelta(days=i)).date()
        chart_labels.append(d.strftime('%b %d'))
        chart_data.append(daily_map.get(d.isoformat(), 0))

    # ── AI Model Inference ────────────────────────────────────────────────────
    from apps.ai_insights.models import AIModel, ModelPrediction

    total_models      = AIModel.objects.count()
    active_models     = AIModel.objects.filter(status='active').count()
    pending_models    = AIModel.objects.filter(status='pending').count()
    total_predictions = ModelPrediction.objects.count()
    predictions_7d    = ModelPrediction.objects.filter(created_at__gte=ago_7d).count()

    models_by_status = list(
        AIModel.objects.values('status').annotate(count=Count('id')).order_by('status')
    )
    models_by_category = list(
        AIModel.objects.values('category').annotate(count=Count('id')).order_by('-count')
    )
    top_models = list(
        AIModel.objects.filter(run_count__gt=0).order_by('-run_count')[:5]
        .values('name', 'category', 'status', 'run_count')
    )

    # Risk distribution from ModelPrediction
    low_risk  = ModelPrediction.objects.filter(risk_score__lt=0.3, risk_score__isnull=False).count()
    med_risk  = ModelPrediction.objects.filter(risk_score__gte=0.3, risk_score__lt=0.7).count()
    high_risk = ModelPrediction.objects.filter(risk_score__gte=0.7).count()

    # ── Platform Overview ─────────────────────────────────────────────────────
    total_users = CustomUser.objects.count()
    users_by_role = list(
        CustomUser.objects.values('role').annotate(count=Count('id')).order_by('role')
    )

    records_by_type = list(
        MedicalRecord.objects.values('record_type').annotate(count=Count('id')).order_by('-count')
    )
    total_records  = MedicalRecord.objects.count()
    records_30d    = MedicalRecord.objects.filter(uploaded_at__gte=ago_30d).count()

    alerts_total    = HealthAlert.objects.count()
    alerts_critical = HealthAlert.objects.filter(severity='critical').count()
    alerts_warning  = HealthAlert.objects.filter(severity='warning').count()
    alerts_info     = HealthAlert.objects.filter(severity='info').count()
    alerts_unread   = HealthAlert.objects.filter(is_read=False).count()

    ctx = {
        # RAG
        'total_queries':          total_queries,
        'queries_7d':             queries_7d,
        'queries_30d':            queries_30d,
        'total_sessions':         total_sessions,
        'avg_response_len':       int(avg_response_len),
        'retrieval_failures':     retrieval_failures,
        'retrieval_failure_pct':  retrieval_failure_pct,
        'provider_counts':        provider_counts,
        'avg_chunks':             round(avg_chunks, 1),
        'total_chunks':           total_chunks,
        'embedded_chunks':        embedded_chunks,
        'embedding_pct':          embedding_pct,
        'total_documents':        total_documents,
        'docs_by_type':           docs_by_type,
        'chart_labels_json':      json.dumps(chart_labels),
        'chart_data_json':        json.dumps(chart_data),
        # AI Models
        'total_models':           total_models,
        'active_models':          active_models,
        'pending_models':         pending_models,
        'total_predictions':      total_predictions,
        'predictions_7d':         predictions_7d,
        'models_by_status':       models_by_status,
        'models_by_category':     models_by_category,
        'top_models':             top_models,
        'low_risk':               low_risk,
        'med_risk':               med_risk,
        'high_risk':              high_risk,
        # Platform
        'total_users':            total_users,
        'users_by_role':          users_by_role,
        'total_records':          total_records,
        'records_30d':            records_30d,
        'records_by_type':        records_by_type,
        'alerts_total':           alerts_total,
        'alerts_critical':        alerts_critical,
        'alerts_warning':         alerts_warning,
        'alerts_info':            alerts_info,
        'alerts_unread':          alerts_unread,
    }
    return render(request, 'dashboard/monitoring.html', ctx)

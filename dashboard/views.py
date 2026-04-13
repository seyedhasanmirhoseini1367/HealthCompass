from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum
from medical_records.models import MedicalRecord
from ai_insights.models import HealthAlert
from notifications.models import Notification
from accounts.models import CustomUser, PatientDoctorRelationship


@login_required
def home(request):
    user = request.user
    ctx = {}

    if user.is_patient:
        ctx['records'] = MedicalRecord.objects.filter(patient=user).order_by('-uploaded_at')[:5]
        ctx['alerts'] = HealthAlert.objects.filter(patient=user, is_read=False)[:5]
        ctx['total_records'] = MedicalRecord.objects.filter(patient=user).count()
        ctx['unread_alerts'] = HealthAlert.objects.filter(patient=user, is_read=False).count()
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
        from ai_insights.models import AIModel
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
        from ai_insights.models import AIModel
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

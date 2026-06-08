from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone

import json
from collections import defaultdict
from .models import TreatmentCourse, TreatmentMilestone, CourseMonitor, TreatmentPhoto, SymptomLog
from medical_records.models import MedicalRecord


# ── List ──────────────────────────────────────────────────────────────────────

@login_required
def course_list(request):
    active    = TreatmentCourse.objects.filter(patient=request.user, status='active')
    paused    = TreatmentCourse.objects.filter(patient=request.user, status='paused')
    completed = TreatmentCourse.objects.filter(patient=request.user, status='completed')
    cancelled = TreatmentCourse.objects.filter(patient=request.user, status='cancelled')

    # Upcoming monitors across all active courses
    upcoming_monitors = (
        CourseMonitor.objects
        .filter(course__patient=request.user, course__status='active')
        .select_related('course')
    )
    due_monitors = [m for m in upcoming_monitors if m.is_due or m.is_due_soon]

    return render(request, 'treatments/list.html', {
        'active':        active,
        'paused':        paused,
        'completed':     completed,
        'cancelled':     cancelled,
        'due_monitors':  due_monitors,
        'total_active':  active.count(),
    })


# ── Create ────────────────────────────────────────────────────────────────────

@login_required
def course_create(request):
    if request.method == 'POST':
        name              = request.POST.get('name', '').strip()
        condition         = request.POST.get('condition', '').strip()
        specialty         = request.POST.get('specialty', 'other')
        doctor_name       = request.POST.get('doctor_name', '').strip()
        start_date        = request.POST.get('start_date') or timezone.now().date()
        expected_end_date = request.POST.get('expected_end_date') or None
        medications       = request.POST.get('medications', '').strip()
        notes             = request.POST.get('notes', '').strip()

        if not name or not condition:
            messages.error(request, 'Name and condition are required.')
            return redirect('treatments:create')

        course = TreatmentCourse.objects.create(
            patient=request.user,
            name=name,
            condition=condition,
            specialty=specialty,
            doctor_name=doctor_name,
            start_date=start_date,
            expected_end_date=expected_end_date if expected_end_date else None,
            medications=medications,
            notes=notes,
        )

        # Pre-create monitors if provided
        biomarkers = request.POST.getlist('monitor_name')
        frequencies = request.POST.getlist('monitor_freq')
        monitor_notes = request.POST.getlist('monitor_note')
        for bname, freq, mnote in zip(biomarkers, frequencies, monitor_notes):
            bname = bname.strip()
            if bname and freq:
                try:
                    CourseMonitor.objects.create(
                        course=course,
                        biomarker_name=bname,
                        frequency_days=int(freq),
                        note=mnote.strip(),
                    )
                except (ValueError, TypeError):
                    pass

        messages.success(request, f'Treatment course "{name}" created.')
        return redirect('treatments:detail', pk=course.pk)

    return render(request, 'treatments/create.html', {
        'specialties': TreatmentCourse.Specialty.choices,
        'today': timezone.now().date(),
    })


# ── Detail / Timeline ─────────────────────────────────────────────────────────

@login_required
def course_detail(request, pk):
    course     = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    milestones = course.milestones.select_related('linked_record').all()
    monitors   = course.monitors.all()

    # Records available to link when adding milestones
    user_records = MedicalRecord.objects.filter(patient=request.user).order_by('-record_date')

    # Photos
    photos = course.photos.all()

    # Symptom chart data — one dataset per symptom name
    all_logs = course.symptom_logs.all()
    symptom_map = defaultdict(list)
    for log in all_logs:
        symptom_map[log.symptom_name].append({
            'date': log.date.isoformat(),
            'severity': log.severity,
            'note': log.note,
            'id': log.pk,
        })
    symptom_chart_json = json.dumps(dict(symptom_map))
    symptom_names = sorted(symptom_map.keys())

    return render(request, 'treatments/detail.html', {
        'course':            course,
        'milestones':        milestones,
        'monitors':          monitors,
        'user_records':      user_records,
        'milestone_types':   TreatmentMilestone.MilestoneType.choices,
        'outcome_types':     TreatmentMilestone.Outcome.choices,
        'photos':            photos,
        'symptom_logs':      all_logs,
        'symptom_chart_json': symptom_chart_json,
        'symptom_names':     symptom_names,
        'today':             timezone.now().date(),
    })


# ── Add Milestone ─────────────────────────────────────────────────────────────

@login_required
def add_milestone(request, pk):
    course = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)

    if request.method == 'POST':
        title          = request.POST.get('title', '').strip()
        date           = request.POST.get('date') or timezone.now().date()
        milestone_type = request.POST.get('milestone_type', 'other')
        outcome        = request.POST.get('outcome', 'pending')
        note           = request.POST.get('note', '').strip()
        record_id      = request.POST.get('linked_record') or None

        if not title:
            messages.error(request, 'Title is required.')
            return redirect('treatments:detail', pk=pk)

        linked_record = None
        if record_id:
            try:
                linked_record = MedicalRecord.objects.get(pk=record_id, patient=request.user)
            except MedicalRecord.DoesNotExist:
                pass

        TreatmentMilestone.objects.create(
            course=course,
            title=title,
            date=date,
            milestone_type=milestone_type,
            outcome=outcome,
            note=note,
            linked_record=linked_record,
        )
        messages.success(request, 'Milestone added.')

    return redirect('treatments:detail', pk=pk)


# ── Delete Milestone ──────────────────────────────────────────────────────────

@login_required
def delete_milestone(request, pk, mid):
    course    = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    milestone = get_object_or_404(TreatmentMilestone, pk=mid, course=course)
    if request.method == 'POST':
        milestone.delete()
        messages.success(request, 'Milestone removed.')
    return redirect('treatments:detail', pk=pk)


# ── Add Monitor ───────────────────────────────────────────────────────────────

@login_required
def add_monitor(request, pk):
    course = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)

    if request.method == 'POST':
        biomarker_name = request.POST.get('biomarker_name', '').strip()
        frequency_days = request.POST.get('frequency_days', '').strip()
        note           = request.POST.get('note', '').strip()

        if biomarker_name and frequency_days:
            try:
                CourseMonitor.objects.create(
                    course=course,
                    biomarker_name=biomarker_name,
                    frequency_days=int(frequency_days),
                    note=note,
                )
                messages.success(request, f'Now monitoring {biomarker_name}.')
            except (ValueError, TypeError):
                messages.error(request, 'Invalid frequency.')

    return redirect('treatments:detail', pk=pk)


# ── Mark Monitor Checked ──────────────────────────────────────────────────────

@login_required
def mark_monitor_checked(request, pk, mid):
    course  = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    monitor = get_object_or_404(CourseMonitor, pk=mid, course=course)
    if request.method == 'POST':
        monitor.last_checked = timezone.now().date()
        monitor.save()
        messages.success(request, f'{monitor.biomarker_name} marked as checked today.')
    return redirect('treatments:detail', pk=pk)


# ── Delete Monitor ────────────────────────────────────────────────────────────

@login_required
def delete_monitor(request, pk, mid):
    course  = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    monitor = get_object_or_404(CourseMonitor, pk=mid, course=course)
    if request.method == 'POST':
        monitor.delete()
    return redirect('treatments:detail', pk=pk)


# ── Change Status ─────────────────────────────────────────────────────────────

@login_required
def set_status(request, pk):
    course = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in dict(TreatmentCourse.Status.choices):
            course.status = new_status
            course.save()
            messages.success(request, f'Course marked as {course.get_status_display()}.')
    return redirect('treatments:detail', pk=pk)


# ── Delete Course ─────────────────────────────────────────────────────────────

@login_required
def delete_course(request, pk):
    course = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    if request.method == 'POST':
        name = course.name
        course.delete()
        messages.success(request, f'"{name}" deleted.')
        return redirect('treatments:list')
    return redirect('treatments:detail', pk=pk)


# ── Photos ────────────────────────────────────────────────────────────────────

@login_required
def add_photo(request, pk):
    course = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    if request.method == 'POST':
        image = request.FILES.get('image')
        if not image:
            messages.error(request, 'Please select an image file.')
            return redirect('treatments:detail', pk=pk)
        date      = request.POST.get('date') or timezone.now().date()
        caption   = request.POST.get('caption', '').strip()
        body_area = request.POST.get('body_area', '').strip()
        TreatmentPhoto.objects.create(
            course=course, image=image,
            date=date, caption=caption, body_area=body_area,
        )
        messages.success(request, 'Photo added.')
    return redirect('treatments:detail', pk=pk)


@login_required
def delete_photo(request, pk, photo_id):
    course = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    photo  = get_object_or_404(TreatmentPhoto, pk=photo_id, course=course)
    if request.method == 'POST':
        photo.image.delete(save=False)
        photo.delete()
        messages.success(request, 'Photo removed.')
    return redirect('treatments:detail', pk=pk)


# ── Symptom logs ──────────────────────────────────────────────────────────────

@login_required
def add_symptom_log(request, pk):
    course = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    if request.method == 'POST':
        symptom_name = request.POST.get('symptom_name', '').strip()
        severity     = request.POST.get('severity', '').strip()
        date         = request.POST.get('date') or timezone.now().date()
        note         = request.POST.get('note', '').strip()
        if symptom_name and severity:
            try:
                sev = int(severity)
                if 1 <= sev <= 10:
                    SymptomLog.objects.create(
                        course=course, symptom_name=symptom_name,
                        severity=sev, date=date, note=note,
                    )
                    messages.success(request, 'Symptom logged.')
                else:
                    messages.error(request, 'Severity must be between 1 and 10.')
            except (ValueError, TypeError):
                messages.error(request, 'Invalid severity value.')
        else:
            messages.error(request, 'Symptom name and severity are required.')
    return redirect('treatments:detail', pk=pk)


@login_required
def delete_symptom_log(request, pk, log_id):
    course = get_object_or_404(TreatmentCourse, pk=pk, patient=request.user)
    log    = get_object_or_404(SymptomLog, pk=log_id, course=course)
    if request.method == 'POST':
        log.delete()
    return redirect('treatments:detail', pk=pk)

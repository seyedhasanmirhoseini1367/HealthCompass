import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Appointment
from treatments.models import TreatmentCourse


@login_required
def appointment_list(request):
    today    = timezone.now().date()
    upcoming = Appointment.objects.filter(
        patient=request.user,
        date__gte=today,
        status__in=['scheduled', 'rescheduled'],
    ).order_by('date', 'time')

    overdue = Appointment.objects.filter(
        patient=request.user, date__lt=today, status='scheduled',
    ).order_by('date')

    past = Appointment.objects.filter(patient=request.user).exclude(
        date__gte=today, status__in=['scheduled', 'rescheduled']
    ).exclude(
        date__lt=today, status='scheduled'
    ).order_by('-date')[:40]

    courses = TreatmentCourse.objects.filter(patient=request.user, status='active')

    return render(request, 'appointments/list.html', {
        'upcoming':       upcoming,
        'overdue':        overdue,
        'past':           past,
        'courses':        courses,
        'today':          today,
        'type_choices':   Appointment.AppointmentType.choices,
        'status_choices': Appointment.Status.choices,
    })


@login_required
def add_appointment(request):
    if request.method == 'POST':
        title     = request.POST.get('title', '').strip()
        apt_type  = request.POST.get('appointment_type', 'general')
        doctor    = request.POST.get('doctor_name', '').strip()
        specialty = request.POST.get('specialty', '').strip()
        location  = request.POST.get('location', '').strip()
        date_str  = request.POST.get('date', '').strip()
        time_str  = request.POST.get('time', '').strip()
        notes     = request.POST.get('notes', '').strip()
        course_id = request.POST.get('linked_course') or None

        if title and date_str:
            try:
                apt = Appointment.objects.create(
                    patient=request.user, title=title,
                    appointment_type=apt_type, doctor_name=doctor,
                    specialty=specialty, location=location,
                    date=datetime.date.fromisoformat(date_str),
                    time=datetime.time.fromisoformat(time_str) if time_str else None,
                    notes=notes,
                )
                if course_id:
                    try:
                        apt.linked_course = TreatmentCourse.objects.get(pk=course_id, patient=request.user)
                        apt.save()
                    except TreatmentCourse.DoesNotExist:
                        pass
                messages.success(request, f'Appointment "{title}" added.')
            except (ValueError, TypeError) as exc:
                messages.error(request, f'Invalid date or time: {exc}')
        else:
            messages.error(request, 'Title and date are required.')
    return redirect('appointments:list')


@login_required
def edit_appointment(request, pk):
    apt = get_object_or_404(Appointment, pk=pk, patient=request.user)
    if request.method == 'POST':
        apt.title            = request.POST.get('title', apt.title).strip()
        apt.appointment_type = request.POST.get('appointment_type', apt.appointment_type)
        apt.doctor_name      = request.POST.get('doctor_name', '').strip()
        apt.specialty        = request.POST.get('specialty', '').strip()
        apt.location         = request.POST.get('location', '').strip()
        apt.notes            = request.POST.get('notes', '').strip()
        apt.outcome          = request.POST.get('outcome', '').strip()
        apt.status           = request.POST.get('status', apt.status)
        date_str = request.POST.get('date', '')
        time_str = request.POST.get('time', '')
        try:
            if date_str:
                apt.date = datetime.date.fromisoformat(date_str)
            apt.time = datetime.time.fromisoformat(time_str) if time_str else None
        except ValueError:
            pass
        apt.save()
        messages.success(request, f'"{apt.title}" updated.')
    return redirect('appointments:list')


@login_required
def delete_appointment(request, pk):
    apt = get_object_or_404(Appointment, pk=pk, patient=request.user)
    if request.method == 'POST':
        title = apt.title
        apt.delete()
        messages.success(request, f'"{title}" deleted.')
    return redirect('appointments:list')


@login_required
@require_POST
def mark_status(request, pk):
    apt = get_object_or_404(Appointment, pk=pk, patient=request.user)
    new_status = request.POST.get('status')
    if new_status in dict(Appointment.Status.choices):
        apt.status  = new_status
        apt.outcome = request.POST.get('outcome', apt.outcome).strip()
        apt.save()
    return redirect('appointments:list')

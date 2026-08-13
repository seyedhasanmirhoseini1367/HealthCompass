from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.utils import timezone
from .models import Appointment


@login_required
def appointment_list(request):
    now = timezone.now()
    upcoming = Appointment.objects.filter(
        patient=request.user,
        is_cancelled=False,
        appointment_datetime__gte=now,
    )
    past = Appointment.objects.filter(
        patient=request.user,
        appointment_datetime__lt=now,
    ).order_by('-appointment_datetime')[:20]
    return render(request, 'appointments/list.html', {
        'upcoming': upcoming,
        'past': past,
    })


@login_required
def appointment_create(request):
    if request.method == 'POST':
        appt = _save_appointment(request, None)
        if appt:
            messages.success(request, 'Appointment scheduled.')
            return redirect('appointments:list')
    return render(request, 'appointments/form.html', {'appt': None})


@login_required
def appointment_edit(request, pk):
    appt = get_object_or_404(Appointment, pk=pk, patient=request.user)
    if request.method == 'POST':
        updated = _save_appointment(request, appt)
        if updated:
            messages.success(request, 'Appointment updated.')
            return redirect('appointments:list')
    return render(request, 'appointments/form.html', {'appt': appt})


@login_required
def appointment_delete(request, pk):
    appt = get_object_or_404(Appointment, pk=pk, patient=request.user)
    if request.method == 'POST':
        appt.delete()
        messages.success(request, 'Appointment deleted.')
        return redirect('appointments:list')
    return render(request, 'appointments/delete.html', {'appt': appt})


def _save_appointment(request, instance):
    from datetime import datetime
    title    = request.POST.get('title', '').strip()
    dt_str   = request.POST.get('appointment_datetime', '').strip()
    if not title or not dt_str:
        messages.error(request, 'Title and date/time are required.')
        return None
    # DST note: an external audit flagged this as crashing twice a year, on the
    # basis that make_aware raises AmbiguousTimeError / NonExistentTimeError at
    # the Europe/Helsinki transitions. That was true under pytz. Django 5 uses
    # zoneinfo, which resolves both cases via `fold` and raises nothing —
    # verified on 5.2.12 for the 2027 spring-forward gap and the 2026 autumn
    # fallback, both of which return valid aware datetimes. No handling is
    # needed here, and adding it would be dead code. Revisit only if the project
    # ever pins a Django that reintroduces those exceptions.
    try:
        naive = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
        appt_dt = timezone.make_aware(naive)
    except ValueError:
        messages.error(request, 'Invalid date/time format.')
        return None

    # Length validation. Django does not enforce max_length on save(), so an
    # over-long title reached PostgreSQL and raised DataError -> 500. There is
    # no Form class on this path, unlike every other write path in the project.
    fields = {
        'title':       (title,                                        255),
        'doctor_name': (request.POST.get('doctor_name', '').strip(),  255),
        'location':    (request.POST.get('location', '').strip(),     500),
    }
    too_long = [name for name, (value, limit) in fields.items() if len(value) > limit]
    if too_long:
        messages.error(
            request,
            'Too long: ' + ', '.join(
                f'{n} (max {fields[n][1]} characters)' for n in sorted(too_long)) + '.')
        return None

    if instance is None:
        instance = Appointment(patient=request.user)

    # Only reset the sent-flags when the time actually MOVED. Resetting on every
    # save meant editing a note re-armed reminders that had already fired, so the
    # patient was notified twice for one appointment.
    time_changed = (instance.pk is None or instance.appointment_datetime != appt_dt)

    instance.title               = title
    instance.doctor_name         = fields['doctor_name'][0]
    instance.location            = fields['location'][0]
    instance.appointment_datetime = appt_dt
    instance.notes               = request.POST.get('notes', '').strip()
    instance.remind_24h          = 'remind_24h' in request.POST
    instance.remind_3h           = 'remind_3h'  in request.POST
    instance.remind_2h           = 'remind_2h'  in request.POST
    instance.remind_1h           = 'remind_1h'  in request.POST

    if time_changed:
        instance.reminded_24h = False
        instance.reminded_3h  = False
        instance.reminded_2h  = False
        instance.reminded_1h  = False

    instance.save()
    return instance

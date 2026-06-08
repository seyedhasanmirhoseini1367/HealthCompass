import json
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.conf import settings

from .models import (
    CareCircle, CareGiver, DailyCheckIn,
    MedicationSchedule, MedicationLog, EscalationLog, PainLog,
)
from treatments.models import TreatmentCourse


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_circle(user):
    circle, _ = CareCircle.objects.get_or_create(patient=user)
    return circle


def _touch_active(circle):
    """Update the passive heartbeat timestamp."""
    circle.last_active_at = timezone.now()
    circle.save(update_fields=['last_active_at'])


def _build_adherence_grid(patient, days=14):
    """Return {med_id: {name, dosage, time, days:[{date,status}]}} for last N days."""
    today  = timezone.now().date()
    period = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    meds   = MedicationSchedule.objects.filter(patient=patient, is_active=True)
    grid   = {}
    for med in meds:
        logs = {log.date: log.taken for log in med.logs.filter(date__in=period)}
        grid[med.pk] = {
            'name':   med.name,
            'dosage': med.dosage,
            'time':   med.get_time_of_day_display(),
            'days':   [{'date': d, 'status': logs.get(d)} for d in period],
        }
    return grid, period


def _site_url(request):
    return getattr(settings, 'SITE_URL', request.build_absolute_uri('/').rstrip('/'))


# ── Patient: Care Hub ─────────────────────────────────────────────────────────

@login_required
def care_home(request):
    circle = _get_or_create_circle(request.user)
    _touch_active(circle)

    today = timezone.now().date()

    # Today's check-in
    checkin_today = DailyCheckIn.objects.filter(patient=request.user, date=today).first()

    # Recent check-ins (last 30 days)
    recent_checkins = DailyCheckIn.objects.filter(patient=request.user).order_by('-date')[:30]

    # Chart data: feeling score trend
    chart_data = [
        {'date': str(c.date), 'feeling': c.feeling_score, 'pain': c.pain_score}
        for c in reversed(list(recent_checkins))
        if c.feeling_score
    ]

    # Medications + 7-day adherence grid
    adh_grid, period = _build_adherence_grid(request.user, days=7)
    medications = MedicationSchedule.objects.filter(patient=request.user, is_active=True)

    # Treatment courses
    courses = TreatmentCourse.objects.filter(patient=request.user, status='active')

    # Caregivers
    caregivers = circle.caregivers.filter(is_active=True)

    # Flags
    flagged = DailyCheckIn.objects.filter(patient=request.user, is_flagged=True).order_by('-date')[:5]

    # Recent escalation logs (for settings tab)
    recent_escalations = EscalationLog.objects.filter(
        patient=request.user
    ).select_related('caregiver').order_by('-sent_at')[:20]

    # Quick "I'm fine" URL
    quick_url = f'{_site_url(request)}/care/quick/{circle.quick_token}/'

    # Pain logs — last 30 days
    pain_logs = PainLog.objects.filter(patient=request.user).order_by('-date', '-created_at')[:30]
    pain_area_choices = PainLog.BODY_AREAS
    pain_type_choices = PainLog.PAIN_TYPES

    return render(request, 'care/home.html', {
        'circle':              circle,
        'checkin_today':       checkin_today,
        'recent_checkins':     recent_checkins,
        'chart_json':          json.dumps(chart_data),
        'adh_grid':            adh_grid,
        'period':              period,
        'medications':         medications,
        'courses':             courses,
        'caregivers':          caregivers,
        'flagged':             flagged,
        'today':               today,
        'mood_choices':        DailyCheckIn.MOOD_CHOICES,
        'time_choices':        MedicationSchedule.TimeOfDay.choices,
        'rel_choices':         CareGiver.Relationship.choices,
        'recent_escalations':  recent_escalations,
        'quick_url':           quick_url,
        'HOURS':               list(range(24)),
        'pain_logs':           pain_logs,
        'pain_area_choices':   pain_area_choices,
        'pain_type_choices':   pain_type_choices,
    })


# ── Check-in ──────────────────────────────────────────────────────────────────

@login_required
def do_checkin(request):
    if request.method != 'POST':
        return redirect('care:home')

    today = timezone.now().date()
    checkin, created = DailyCheckIn.objects.get_or_create(
        patient=request.user, date=today
    )

    feeling  = request.POST.get('feeling_score')
    pain     = request.POST.get('pain_score')
    sleep    = request.POST.get('sleep_score')
    mood     = request.POST.get('mood', '')
    symptoms = request.POST.get('symptoms', '').strip()
    meds     = request.POST.get('medications_taken')

    checkin.feeling_score     = int(feeling) if feeling else None
    checkin.pain_score        = int(pain)    if pain    else None
    checkin.sleep_score       = int(sleep)   if sleep   else None
    checkin.mood              = mood
    checkin.symptoms          = symptoms
    checkin.medications_taken = (meds == 'yes') if meds else None
    checkin.is_quick          = False
    checkin.auto_flag()
    checkin.save()

    messages.success(request, 'Check-in saved. Thank you!')
    return redirect('care:home')


# ── Medications ───────────────────────────────────────────────────────────────

@login_required
def add_medication(request):
    if request.method == 'POST':
        name        = request.POST.get('name', '').strip()
        dosage      = request.POST.get('dosage', '').strip()
        time_of_day = request.POST.get('time_of_day', 'morning')
        notes       = request.POST.get('notes', '').strip()
        course_id   = request.POST.get('linked_course') or None

        if name:
            med = MedicationSchedule.objects.create(
                patient=request.user,
                name=name, dosage=dosage,
                time_of_day=time_of_day, notes=notes,
            )
            if course_id:
                try:
                    med.linked_course = TreatmentCourse.objects.get(
                        pk=course_id, patient=request.user)
                    med.save()
                except TreatmentCourse.DoesNotExist:
                    pass
            messages.success(request, f'"{name}" added to your medication schedule.')
        else:
            messages.error(request, 'Medication name is required.')
    return redirect('care:home')


@login_required
def delete_medication(request, pk):
    med = get_object_or_404(MedicationSchedule, pk=pk, patient=request.user)
    if request.method == 'POST':
        med.is_active = False
        med.save()
        messages.success(request, f'"{med.name}" removed.')
    return redirect('care:home')


@login_required
def log_medication(request, pk):
    """Toggle taken/missed for a medication on a given date."""
    med = get_object_or_404(MedicationSchedule, pk=pk, patient=request.user)
    if request.method == 'POST':
        log_date = request.POST.get('date') or timezone.now().date()
        taken    = request.POST.get('taken') == 'true'
        MedicationLog.objects.update_or_create(
            schedule=med, date=log_date,
            defaults={'taken': taken},
        )
    return redirect('care:home')


# ── Caregivers ────────────────────────────────────────────────────────────────

@login_required
def add_caregiver(request):
    if request.method == 'POST':
        circle = _get_or_create_circle(request.user)
        name   = request.POST.get('name', '').strip()
        email  = request.POST.get('email', '').strip()
        rel    = request.POST.get('relationship', 'other')
        if name:
            cg = CareGiver.objects.create(
                circle=circle, name=name, email=email, relationship=rel)
            messages.success(request,
                f'{name} added. Share this link: '
                f'{request.build_absolute_uri(f"/care/view/{cg.access_token}/")}')
        else:
            messages.error(request, 'Name is required.')
    return redirect('care:home')


@login_required
def remove_caregiver(request, pk):
    circle = _get_or_create_circle(request.user)
    cg     = get_object_or_404(CareGiver, pk=pk, circle=circle)
    if request.method == 'POST':
        cg.is_active = False
        cg.save()
        messages.success(request, f'{cg.name} removed from your care circle.')
    return redirect('care:home')


# ── Care Settings ─────────────────────────────────────────────────────────────

@login_required
def care_settings(request):
    """Update circle schedule + per-caregiver notification prefs."""
    if request.method != 'POST':
        return redirect('care:home')

    circle = _get_or_create_circle(request.user)
    action = request.POST.get('action')

    if action == 'circle_settings':
        try:
            circle.usual_checkin_hour   = max(0, min(23, int(request.POST.get('usual_hour', 9))))
            circle.checkin_window_hours = max(1, min(12, int(request.POST.get('window_hours', 2))))
        except (ValueError, TypeError):
            pass
        circle.daily_summary_enabled = request.POST.get('daily_summary') == 'on'
        circle.save()
        messages.success(request, 'Check-in schedule saved.')

    elif action == 'caregiver_settings':
        cg_id = request.POST.get('caregiver_id')
        cg = get_object_or_404(CareGiver, pk=cg_id, circle=circle)
        cg.phone_number   = request.POST.get('phone_number', '').strip()
        cg.notify_email   = request.POST.get('notify_email') == 'on'
        cg.notify_sms     = request.POST.get('notify_sms') == 'on'
        try:
            cg.notify_priority = max(1, min(9, int(request.POST.get('priority', 1))))
        except (ValueError, TypeError):
            cg.notify_priority = 1
        qs = request.POST.get('quiet_start', '').strip()
        qe = request.POST.get('quiet_end',   '').strip()
        cg.quiet_start = int(qs) if qs.isdigit() else None
        cg.quiet_end   = int(qe) if qe.isdigit() else None
        cg.save()
        messages.success(request, f"Notification settings for {cg.name} saved.")

    return redirect('care:home')


# ── Caregiver dashboard (public — token-based) ────────────────────────────────

def caregiver_view(request, token):
    cg = get_object_or_404(CareGiver, access_token=token, is_active=True)

    cg.last_viewed = timezone.now()
    cg.save(update_fields=['last_viewed'])

    patient = cg.circle.patient
    today   = timezone.now().date()

    checkin_today = DailyCheckIn.objects.filter(patient=patient, date=today).first()
    recent = list(DailyCheckIn.objects.filter(patient=patient).order_by('-date')[:30])

    chart_labels   = [str(c.date) for c in reversed(recent) if c.feeling_score]
    chart_feeling  = [c.feeling_score for c in reversed(recent) if c.feeling_score]
    chart_pain, chart_pain_labels = [], []
    for c in reversed(recent):
        if c.pain_score is not None:
            chart_pain_labels.append(str(c.date))
            chart_pain.append(c.pain_score)

    adh_grid, period = _build_adherence_grid(patient, days=14)
    for v in adh_grid.values():
        logged = [d for d in v['days'] if d['status'] is not None]
        taken  = [d for d in v['days'] if d['status'] is True]
        v['adherence_pct'] = round(len(taken) / len(logged) * 100) if logged else None

    flags = DailyCheckIn.objects.filter(patient=patient, is_flagged=True).order_by('-date')[:10]

    missed_count = MedicationLog.objects.filter(
        schedule__patient=patient,
        date__gte=today - timedelta(days=7),
        taken=False,
    ).count()

    # Pending unacknowledged escalation alerts
    pending_escalations = EscalationLog.objects.filter(
        patient=patient,
        acknowledged_at__isnull=True,
        is_dismissed=False,
        channel__in=['email', 'sms'],
    ).order_by('-sent_at')[:5]

    # Last seen / active at
    circle = cg.circle
    last_active = circle.last_active_at

    return render(request, 'care/caregiver_dashboard.html', {
        'caregiver':           cg,
        'patient':             patient,
        'checkin_today':       checkin_today,
        'recent':              recent,
        'chart_labels':        json.dumps(chart_labels),
        'chart_feeling':       json.dumps(chart_feeling),
        'chart_pain':          json.dumps(chart_pain),
        'adh_grid':            adh_grid,
        'period':              period,
        'flags':               flags,
        'missed_count':        missed_count,
        'today':               today,
        'pending_escalations': pending_escalations,
        'last_active':         last_active,
    })


# ── Escalation: acknowledge ───────────────────────────────────────────────────

def acknowledge_by_token(request, token):
    """No-login one-click acknowledge from email link (GET is fine — token is secret)."""
    log = get_object_or_404(EscalationLog, acknowledge_token=token)
    name = log.caregiver.name if log.caregiver else 'Caregiver'
    if not log.is_acknowledged:
        log.acknowledge(by=name)
    return render(request, 'care/acknowledged.html', {
        'log':          log,
        'patient_name': log.patient.get_full_name() or log.patient.username,
        'caregiver_name': name,
    })


@login_required
@require_POST
def acknowledge_escalation(request, pk):
    """AJAX acknowledge from the patient's own settings panel."""
    log = get_object_or_404(EscalationLog, pk=pk, patient=request.user)
    log.acknowledge(by=request.user.get_full_name() or request.user.username)
    return JsonResponse({'ok': True, 'acknowledged_by': log.acknowledged_by})


# ── Quick "I'm fine" check-in (no login) ─────────────────────────────────────

def quick_checkin(request, token):
    """
    No-login page: patient taps one link → DailyCheckIn created with is_quick=True.
    Stops the escalation cycle for the day.
    """
    circle = get_object_or_404(CareCircle, quick_token=token, is_active=True)
    _touch_active(circle)

    today = timezone.now().date()
    checkin, created = DailyCheckIn.objects.get_or_create(
        patient=circle.patient,
        date=today,
        defaults={'is_quick': True},
    )

    patient_name = circle.patient.get_full_name() or circle.patient.username

    return render(request, 'care/quick_checkin.html', {
        'circle':       circle,
        'patient_name': patient_name,
        'created':      created,
        'already_full': not created and not checkin.is_quick,
    })


# ── Voice Check-in ────────────────────────────────────────────────────────────

@login_required
def voice_checkin(request):
    medications = MedicationSchedule.objects.filter(
        patient=request.user, is_active=True
    ).values_list('name', flat=True)
    return render(request, 'care/voice_checkin.html', {
        'today':       timezone.now().date(),
        'medications': list(medications),
    })


@login_required
@require_POST
def voice_save(request):
    """Receive structured data from the voice agent and save as DailyCheckIn."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    today = timezone.now().date()
    obj, _ = DailyCheckIn.objects.get_or_create(patient=request.user, date=today)

    def _int(val, lo=0, hi=10):
        try:
            v = int(val)
            return v if lo <= v <= hi else None
        except (TypeError, ValueError):
            return None

    obj.feeling_score     = _int(data.get('feeling_score'), 1, 10)
    obj.pain_score        = _int(data.get('pain_score'),    0, 10)
    obj.sleep_score       = _int(data.get('sleep_score'),   1, 10)
    obj.mood              = data.get('mood') or ''
    obj.symptoms          = data.get('symptoms') or ''
    obj.is_quick          = False
    meds_val              = data.get('medications_taken')
    obj.medications_taken = bool(meds_val) if meds_val is not None else None
    obj.auto_flag()
    obj.save()

    return JsonResponse({
        'ok':          True,
        'flagged':     obj.is_flagged,
        'flag_reason': obj.flag_reason,
        'feeling':     obj.feeling_score,
        'pain':        obj.pain_score,
        'sleep':       obj.sleep_score,
        'mood':        obj.mood,
    })


# ── Pain Body Map ─────────────────────────────────────────────────────────────

@login_required
@require_POST
def add_pain_log(request):
    """Log a pain entry from the body map."""
    body_area = request.POST.get('body_area', '').strip()
    pain_type = request.POST.get('pain_type', '').strip()
    severity  = request.POST.get('severity', '').strip()
    trigger   = request.POST.get('trigger', '').strip()
    notes     = request.POST.get('notes', '').strip()
    date_str  = request.POST.get('date', '')

    valid_areas = [a[0] for a in PainLog.BODY_AREAS]
    if body_area not in valid_areas:
        messages.error(request, 'Invalid body area.')
        return redirect('care:home')

    try:
        sev = int(severity)
        if not 1 <= sev <= 10:
            raise ValueError
    except (ValueError, TypeError):
        messages.error(request, 'Severity must be 1–10.')
        return redirect('care:home')

    import datetime
    try:
        log_date = datetime.date.fromisoformat(date_str) if date_str else timezone.now().date()
    except ValueError:
        log_date = timezone.now().date()

    PainLog.objects.create(
        patient=request.user,
        date=log_date,
        body_area=body_area,
        pain_type=pain_type,
        severity=sev,
        trigger=trigger,
        notes=notes,
    )
    messages.success(request, 'Pain entry logged.')
    return redirect('care:home')


@login_required
@require_POST
def delete_pain_log(request, pk):
    log = get_object_or_404(PainLog, pk=pk, patient=request.user)
    log.delete()
    return redirect('care:home')


# ── Medication Interaction Checker ───────────────────────────────────────────

@login_required
def check_interactions(request):
    """
    AI-powered medication interaction check.
    Returns JSON: {interactions: [{meds, severity, description}], error?}
    """
    meds = list(
        MedicationSchedule.objects.filter(
            patient=request.user, is_active=True
        ).values_list('name', 'dosage')
    )

    if len(meds) < 2:
        return JsonResponse({'interactions': [], 'note': 'Need at least 2 medications to check.'})

    med_list = ', '.join(
        f"{name} {dosage}".strip() for name, dosage in meds
    )

    prompt = f"""You are a clinical pharmacist. Check the following medications for known drug interactions.
Medications: {med_list}

For each significant interaction found, reply with a JSON array of objects with these fields:
- "meds": list of 2 medication names involved
- "severity": one of "mild", "moderate", "severe"
- "description": 1–2 sentences explaining the interaction and what to watch for

If no significant interactions exist, return an empty array [].
Respond ONLY with the JSON array, no other text."""

    result = []
    error  = None

    # Try Gemini → Anthropic → OpenAI in order
    try:
        import google.generativeai as genai
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model    = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        text     = response.text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        result = json.loads(text)
    except Exception:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            msg    = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=600,
                messages=[{'role': 'user', 'content': prompt}],
            )
            text   = msg.content[0].text.strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0]
            result = json.loads(text)
        except Exception as exc:
            error = str(exc)

    return JsonResponse({'interactions': result, 'error': error})

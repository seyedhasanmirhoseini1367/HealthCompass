"""
The three hub pages the navigation collapses into.

Why hubs at all
---------------
The sidebar had grown to twelve destinations, and the user was being asked to
hold the application's internal structure in their head: Medications was a
document-derived list, My Care was today's doses, Watching Over was inbound
sharing, Sharing was outbound sharing, Notifications was a log. Five entries for
what a person experiences as two questions — "how am I?" and "who needs me?".

So the capabilities did not change; their entry points did. Each hub is a thin
composition over services that already existed:

    My Health  -> ai_insights, measurements, care reminders
    Care       -> care.models, accounts.authz, appointments
    Settings   -> accounts views that were already there

Nothing here queries a patient other than `request.user`, and nothing here
decides authorization. The one place a hub touches another person's data — the
people list in Care — goes through `accounts.authz.shared_with`, the same seam
the notification pipeline and the person page use.
"""
from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

logger = logging.getLogger(__name__)


@login_required
def my_health(request):
    """
    What is true about me clinically, in one place.

    Deliberately NOT a list of links: each section carries its current state, so
    the page answers "is anything off?" without another click. Detail still
    lives on the pages that own it.
    """
    from apps.ai_insights.models import HealthAlert, ModelPrediction
    from apps.dashboard.overview import recent_measurements
    from apps.medical_records.models import MedicalRecord

    alerts = list(HealthAlert.objects.filter(patient=request.user)
                  .order_by('-created_at')[:5])

    # Reminders live here now. A dose you take is a fact about you; it read
    # oddly on Companions, whose subject is somebody else.
    from datetime import timedelta

    from apps.care.models import CareTask, PatientReport, TaskOccurrence

    now = timezone.now()
    occurrences = (TaskOccurrence.objects
                   .filter(patient=request.user,
                           due_at__gte=now - timedelta(days=1),
                           due_at__lte=now + timedelta(days=1))
                   .select_related('task').order_by('due_at'))

    return render(request, 'dashboard/my_health.html', {
        'occurrences':   occurrences,
        'tasks':         CareTask.objects.filter(patient=request.user,
                                                 is_active=True),
        'states':        TaskOccurrence.State,
        # Built from HUMAN_STATES so a button the model would reject cannot
        # appear on the page.
        'answers':       [(s.value, s.label) for s in TaskOccurrence.HUMAN_STATES],
        'reports':       PatientReport.objects.filter(patient=request.user)[:5],
        'alerts':        alerts,
        'unread_alerts': sum(1 for a in alerts if not a.is_read),
        'measurements':  recent_measurements(request.user, limit=6),
        'record_count':  MedicalRecord.objects.filter(patient=request.user).count(),
        'recent_records': MedicalRecord.objects.filter(patient=request.user)
                          .order_by('-uploaded_at')[:5],
        'predictions':   ModelPrediction.objects.filter(patient=request.user)
                         .order_by('-created_at')[:3],
    })


@login_required
def settings_hub(request):
    """
    Everything that configures the account, gathered rather than scattered.

    Privacy, sharing and the emergency card were reachable only from a dropdown
    or the mobile menu. They are the controls with the largest consequences in
    this product — who can see a patient's health data — and they were the
    hardest to find.
    """
    from apps.accounts.models import SharingGrant

    outbound = SharingGrant.objects.filter(patient=request.user).count()
    active_outbound = sum(
        1 for g in SharingGrant.objects.filter(patient=request.user)
        if g.is_effective)

    return render(request, 'dashboard/settings_hub.html', {
        'share_count':        outbound,
        'active_share_count': active_outbound,
        'emergency_enabled':  _emergency_card_enabled(request.user),
    })


def _emergency_card_enabled(user) -> bool:
    profile = getattr(user, 'patient_profile', None)
    return bool(getattr(profile, 'emergency_card_enabled', False))


# ── Care hub ──────────────────────────────────────────────────────────────────

def care_overview(request):
    """
    Data for the Companions page: who I look after, and who can see me.

    Both directions of sharing are here because a person experiences them as one
    subject. "Who can see me" and "who can I see" were two pages with different
    names for the same relationship viewed from opposite ends.

    Deliberately nothing else. Reminders moved to My Health, and appointments
    were dropped entirely — both have their own destination, and echoing them
    here turned the page into a second navigation menu.

    Called from `care.views.hub` rather than being a view itself, so the care app
    keeps ownership of its own URL.
    """
    from apps.accounts.authz import shared_with
    from apps.accounts.models import SharingGrant
    from apps.care.models import MonitoringSignal
    from apps.notifications.recipients import CARE_SCOPE

    user = request.user

    people = []
    for subject in shared_with(user, CARE_SCOPE):
        open_signals = MonitoringSignal.objects.filter(
            patient=subject, resolved_at__isnull=True).count()
        latest = (MonitoringSignal.objects.filter(patient=subject)
                  .order_by('-created_at').first())
        people.append({
            'subject':      subject,
            'open_signals': open_signals,
            'last_activity': latest.created_at if latest else None,
        })
    # Whoever needs something comes first.
    people.sort(key=lambda p: (-p['open_signals'],))

    # Outbound: who I have let see me. Shown alongside, because "Anna can see my
    # alerts" and "I can see Bertta" are the same feature from two ends.
    granted = [g for g in SharingGrant.objects.filter(patient=user)
               .select_related('recipient') if g.is_effective]

    return {
        'people':  people,
        'granted': granted,
    }

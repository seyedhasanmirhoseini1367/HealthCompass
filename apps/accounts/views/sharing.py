"""Sharing with family and carers, and asking someone to share."""
import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST


from ..models import (CustomUser, DoctorAccessLog)
from ..verification import require_verified_email

logger = logging.getLogger(__name__)


# ─── Family / shared access ──────────────────────────────────────────────────
#
# The patient is the only authority here. Every view below scopes to
# request.user as the grantor, so there is no identifier a caller could
# substitute to act on someone else's data — the same shape as the export and
# consent views.

# Sharing is managed on Companions (/care/), next to the people it concerns.
# These views keep their URLs (old links, Settings, the privacy page) and send
# the person back there.
SHARING_PAGE = '/care/#sharing'


@login_required
def my_shares(request):
    """The old Sharing page. Everything it showed now lives on Companions."""
    return redirect(SHARING_PAGE)


def _apply_grant(patient, recipient, scopes):
    """Active grant with exactly *scopes* (answering the recipient's own request)."""
    from ..sharing_service import apply_grant
    return apply_grant(patient, recipient, scopes, activate=True)


def _clean_scopes(request):
    from ..sharing_service import clean_scopes
    return clean_scopes(request.POST.getlist('scopes'))


def _find_user(identifier):
    """Resolve a person by email only (audit SEC-02); see sharing_service."""
    from ..sharing_service import find_account_by_email
    return find_account_by_email(identifier)


@login_required
@require_POST
@require_verified_email
def create_share(request):
    """
    Invite someone to see part of your record. They must accept before they
    see anything (audit SEC-02), and the reply is the same whether or not the
    address has an account (SEC-14). The grantor is always request.user.
    """
    from ..sharing_service import SharingError, offer_share

    try:
        message = offer_share(request.user, request.POST.get('recipient'),
                              request.POST.getlist('scopes'))
    except SharingError as exc:
        if exc.code == 'forbidden':
            raise PermissionDenied from None
        messages.error(request, exc.message)
        return redirect(SHARING_PAGE)
    messages.success(request, message)
    return redirect(SHARING_PAGE)


@login_required
@require_POST
def accept_share(request, pk):
    """The recipient accepts an invitation; only now does the share give anything."""
    from ..sharing_service import SharingError, accept_share as accept

    try:
        grant = accept(request.user, pk)
    except SharingError as exc:
        messages.error(request, exc.message)
        return redirect(SHARING_PAGE)
    messages.success(request, f'You can now see what {_name(grant.patient)} shares with you.')
    return redirect(SHARING_PAGE)


@login_required
@require_POST
def decline_share(request, pk):
    from ..sharing_service import SharingError, decline_share as decline

    try:
        decline(request.user, pk)
    except SharingError as exc:
        messages.error(request, exc.message)
        return redirect(SHARING_PAGE)
    messages.success(request, 'Invitation declined. Nothing was shared.')
    return redirect(SHARING_PAGE)


@login_required
@require_POST
def revoke_share(request, pk):
    """Stop sharing, or withdraw an invitation. Takes effect on the next request."""
    from ..models import SharingGrant
    from ..sharing_service import SharingError, revoke_share as revoke

    grant = get_object_or_404(SharingGrant, pk=pk)
    try:
        revoke(request.user, grant, request.POST.get('reason', ''))
    except SharingError:
        raise PermissionDenied from None
    messages.success(request, 'Sharing stopped.')
    return redirect(SHARING_PAGE)


# ── Asking someone to share ──────────────────────────────────────────────────

def _name(user):
    return user.get_full_name() or user.username


@login_required
@require_POST
@require_verified_email
def request_share(request):
    """
    Ask someone to share with you. Creates a request, never access.

    The patient decides on their own Companions page, picking the scopes.
    """
    from ..models import SharingGrant, ShareRequest
    from apps.notifications.models import Notification

    identifier = (request.POST.get('patient') or '').strip().lower()
    message = (request.POST.get('message') or '').strip()[:300]
    if not identifier:
        messages.error(request, 'Enter the email address of the person you want to ask.')
        return redirect(SHARING_PAGE)

    from ..sharing_service import NEUTRAL_REQUEST_MESSAGE
    neutral = NEUTRAL_REQUEST_MESSAGE.format(email=identifier)
    if identifier == (request.user.email or '').lower():
        messages.error(request, 'You already have access to your own records.')
        return redirect(SHARING_PAGE)
    patient = _find_user(identifier)
    if patient is None:
        # Same reply as a sent request: this form must not reveal who is registered.
        messages.success(request, neutral)
        return redirect(SHARING_PAGE)

    grant = SharingGrant.objects.filter(patient=patient, recipient=request.user).first()
    if grant is not None and grant.is_effective:
        messages.info(request, f'{_name(patient)} already shares with you. '
                               'Only they can change what you see.')
        return redirect(SHARING_PAGE)

    mine = ShareRequest.objects.filter(requester=request.user)
    if mine.filter(patient=patient, status=ShareRequest.Status.PENDING).exists():
        messages.info(request, f'You have already asked {_name(patient)}. '
                               'They will see it on their Companions page.')
        return redirect(SHARING_PAGE)

    cooldown = timezone.now() - timedelta(days=ShareRequest.DECLINE_COOLDOWN_DAYS)
    if mine.filter(patient=patient, status=ShareRequest.Status.DECLINED,
                   responded_at__gte=cooldown).exists():
        messages.error(request, f'You asked {_name(patient)} recently. '
                                'Please wait before asking again.')
        return redirect(SHARING_PAGE)

    if mine.filter(status=ShareRequest.Status.PENDING).count() >= ShareRequest.MAX_PENDING_SENT:
        messages.error(request, 'You have too many requests waiting for an answer. '
                                'Cancel one before sending another.')
        return redirect(SHARING_PAGE)

    ShareRequest.objects.create(requester=request.user, patient=patient, message=message)
    Notification.objects.create(
        user=patient, type=Notification.Type.SYSTEM,
        title=f'{_name(request.user)} asked to see your health information',
        message='Nothing is shared unless you approve. You choose what they can see.',
        link=SHARING_PAGE)
    messages.success(request, neutral)
    return redirect(SHARING_PAGE)


@login_required
@require_POST
def answer_share_request(request, pk):
    """The patient approves (choosing what to share) or declines a request to them."""
    from ..models import ShareRequest
    from apps.notifications.models import Notification

    req = get_object_or_404(ShareRequest, pk=pk, patient=request.user,
                            status=ShareRequest.Status.PENDING)
    decision = request.POST.get('decision')

    if decision == 'approve':
        scopes = _clean_scopes(request)
        if not scopes:
            messages.error(request, 'Choose at least one thing to share, or decline.')
            return redirect(SHARING_PAGE)
        _apply_grant(request.user, req.requester, scopes)
        req.status = ShareRequest.Status.APPROVED
        req.responded_at = timezone.now()
        req.save(update_fields=['status', 'responded_at'])
        from ..sharing_service import scope_labels
        Notification.objects.create(
            user=req.requester, type=Notification.Type.SYSTEM,
            title=f'{_name(request.user)} is now sharing with you',
            message=f'You can see their {scope_labels(scopes)}.',
            link=f'/care/person/{request.user.pk}/')
        messages.success(request, f'{_name(req.requester)} ({req.requester.email}) can now see your '
                                  f'{scope_labels(scopes)}. You can stop this at any time.')
    elif decision == 'decline':
        # No notification to the requester: saying no should cost nothing.
        req.status = ShareRequest.Status.DECLINED
        req.responded_at = timezone.now()
        req.save(update_fields=['status', 'responded_at'])
        messages.success(request, 'Request declined. Nothing was shared.')
    else:
        messages.error(request, 'Choose approve or decline.')
    return redirect(SHARING_PAGE)


@login_required
@require_POST
def cancel_share_request(request, pk):
    """The requester withdraws a request they sent."""
    from ..models import ShareRequest

    req = get_object_or_404(ShareRequest, pk=pk, requester=request.user,
                            status=ShareRequest.Status.PENDING)
    req.status = ShareRequest.Status.CANCELLED
    req.responded_at = timezone.now()
    req.save(update_fields=['status', 'responded_at'])
    messages.success(request, 'Request cancelled.')
    return redirect(SHARING_PAGE)


@login_required
def shared_patient(request, pk):
    """
    What a recipient may actually see of someone who shared with them.

    Every section is gated on its own scope, evaluated here rather than in the
    template: hiding a block in HTML is not authorization, and a template that
    decides what to fetch would put the rule in two places.

    Until this view existed the scopes were unreachable — a grant said "Records"
    on the patient's sharing page while no path served them a record. Failing
    closed is not the same as being correct: the patient was told access existed
    that did not.
    """
    from ..authz import sharing_grant
    from apps.ai_insights.models import HealthAlert
    from apps.appointments.models import Appointment
    from apps.medical_records.models import MedicalRecord

    subject = get_object_or_404(CustomUser, pk=pk)

    # One lookup for the pair, then each scope is read off it. A grant that
    # gives nothing is indistinguishable from no grant at all.
    grant = None
    for scope in ('records', 'alerts', 'appointments'):
        grant = sharing_grant(request.user, subject, scope)
        if grant is not None:
            break
    if grant is None:
        raise Http404

    records = alerts = appointments = None

    if grant.allows('records'):
        qs = MedicalRecord.objects.filter(patient=subject)
        # A frozen share shows the record as it stood, not as it grows. NULL
        # means the share follows the record forward, which is the common case.
        if grant.data_cutoff is not None:
            qs = qs.filter(uploaded_at__lt=grant.data_cutoff)
        records = qs.order_by('-record_date', '-uploaded_at')[:100]

    care_signals = None
    care_activity = None

    if grant.allows('alerts'):
        # Severity and title only — the alert says something is wrong without
        # disclosing the values behind it, which is the whole point of alerts
        # being a scope separate from records.
        alerts = (HealthAlert.objects.filter(patient=subject)
                  .order_by('-created_at')[:20])

        # Care monitoring belongs to the SAME scope, and is shown here rather
        # than on a second page. There used to be two caregiver pages about one
        # person — this one for records/alerts/appointments, /care/watching/<pk>/
        # for care signals — reached from different places, with the scope rule
        # implemented twice. Two pages about one person is how the two answers
        # start disagreeing.
        from apps.care.models import MonitoringSignal

        care_signals = list(
            MonitoringSignal.objects
            .filter(patient=subject, resolved_at__isnull=True)
            .prefetch_related('occurrences', 'reports')
            .order_by('-created_at')[:20])
        care_activity = _care_activity(subject)

    if grant.allows('appointments'):
        appointments = (Appointment.objects
                        .filter(patient=subject, is_cancelled=False)
                        .order_by('appointment_datetime')[:20])

    # Reading someone else's record is an access event, recorded in THEIR trail
    # exactly as a clinician's read is. A patient asking "who has seen my data"
    # must see family members too, not only doctors.
    DoctorAccessLog.objects.create(
        actor=request.user, patient=subject, resource='shared:patient_overview')

    context = {
        'subject':      subject,
        'grant':        grant,
        'records':      records,
        'alerts':       alerts,
        'appointments': appointments,
        'care_signals': [_present_signal(s) for s in (care_signals or [])],
        'has_care_scope': care_signals is not None,
        'care_activity': care_activity,
    }
    return render(request, 'accounts/shared_patient.html', context)


def _care_activity(subject):
    """
    A week of care answers, counted.

    The four states are shown separately and never summed into an adherence
    figure. "Unconfirmed" is the app not hearing back; "missed" is the person
    saying they missed it. Averaging those into one percentage would turn our
    ignorance into their behaviour.
    """
    from datetime import timedelta

    from django.utils import timezone as _tz

    from apps.care.models import TaskOccurrence

    since = _tz.now() - timedelta(days=7)
    occurrences = TaskOccurrence.objects.filter(patient=subject, due_at__gte=since)
    counts = {
        'confirmed':   occurrences.filter(state=TaskOccurrence.State.CONFIRMED).count(),
        'unconfirmed': occurrences.filter(state=TaskOccurrence.State.UNCONFIRMED).count(),
        'missed':      occurrences.filter(state=TaskOccurrence.State.MISSED).count(),
        'skipped':     occurrences.filter(state=TaskOccurrence.State.SKIPPED).count(),
    }
    counts['any'] = any(counts.values())
    return counts


def _present_signal(signal):
    """
    What a caregiver reads about one care signal.

    Says what was observed and stops there. "Not confirmed 3 times" is a fact
    about this application's records; "not taking her medication" would be a
    claim about a person that the evidence — silence — cannot support.
    """
    from apps.care.models import MonitoringSignal

    if signal.kind == MonitoringSignal.Kind.REPEATED_UNCONFIRMED:
        count = signal.occurrences.count()
        first = signal.occurrences.first()
        label = first.task.label if first else 'a care task'
        return {
            'signal':   signal,
            'headline': f'{count} unanswered reminder{"" if count == 1 else "s"}',
            'detail':   f'“{label}” was not confirmed {count} time'
                        f'{"" if count == 1 else "s"} in a row. That means no one '
                        f'answered — not that it was missed.',
        }
    if signal.kind == MonitoringSignal.Kind.REPORTED_MISSED:
        return {'signal': signal, 'headline': 'Reported a missed task',
                'detail': 'They told the app they missed a scheduled task.'}
    if signal.kind == MonitoringSignal.Kind.REPORTED_SYMPTOM:
        entry = signal.reports.first()
        return {'signal': signal, 'headline': 'Reported how they are feeling',
                'detail': entry.text if entry else '', 'is_their_words': True}
    return {'signal': signal, 'headline': 'Needs attention', 'detail': ''}


@login_required
def shared_record(request, pk, record_pk):
    """One shared record. Scope re-checked here, never inherited from the list."""
    from ..authz import sharing_grant
    from apps.medical_records.models import MedicalRecord

    subject = get_object_or_404(CustomUser, pk=pk)
    grant = sharing_grant(request.user, subject, 'records')
    if grant is None:
        raise Http404

    record = get_object_or_404(MedicalRecord, pk=record_pk, patient=subject)
    if grant.data_cutoff is not None and record.uploaded_at >= grant.data_cutoff:
        raise Http404

    DoctorAccessLog.objects.create(
        actor=request.user, patient=subject, resource=f'shared:record:{record.pk}')

    return render(request, 'accounts/shared_record.html', {
        'subject': subject,
        'record':  record,
        # effective() so a corrected value is what the family member sees, the
        # same reading the patient and their clinicians see.
        'lab_values': [(lv, lv.effective()) for lv in record.lab_values.all()],
    })

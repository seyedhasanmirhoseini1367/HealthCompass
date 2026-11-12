import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..serializers import (AppointmentSerializer, HealthAlertSerializer,
                           MedicalRecordSerializer, MonitoringSignalSerializer,
                           SharingGrantSerializer, UserSerializer)
from healthcompass.errors import client_error
from apps.accounts.verification import require_verified_email

_log = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sharing_companions(request):
    """Who I look after, and who can see me. Mirrors dashboard.hubs.care_overview."""
    try:
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
                'subject':       UserSerializer(subject).data,
                'open_signals':  open_signals,
                'last_activity': latest.created_at if latest else None,
            })
        people.sort(key=lambda p: -p['open_signals'])

        granted  = (SharingGrant.objects.filter(patient=user)
                    .select_related('recipient').order_by('-created_at'))
        received = (SharingGrant.objects.filter(recipient=user, status=SharingGrant.Status.ACTIVE)
                    .select_related('patient').order_by('-created_at'))

        return Response({
            'people':   people,
            'granted':  SharingGrantSerializer(granted, many=True).data,
            'received': SharingGrantSerializer([g for g in received if g.is_effective], many=True).data,
        })
    except Exception as exc:
        _log.exception('sharing_companions error: %s', exc)
        return Response(client_error(exc, context='sharing'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@require_verified_email
def create_share(request):
    """Invite someone to see part of your record (they must accept). The reply
    is identical whether or not the address has an account (SEC-02, SEC-14)."""
    from apps.accounts.sharing_service import SharingError, offer_share

    scopes = request.data.get('scopes') or []
    if not isinstance(scopes, list):
        scopes = [scopes]
    try:
        message = offer_share(request.user, request.data.get('identifier'), scopes)
    except SharingError as exc:
        code = status.HTTP_403_FORBIDDEN if exc.code == 'forbidden' else status.HTTP_400_BAD_REQUEST
        return Response({'error': exc.message, 'code': exc.code}, status=code)
    return Response({'detail': message}, status=status.HTTP_202_ACCEPTED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def accept_share(request, pk):
    """The recipient accepts an invitation."""
    from apps.accounts.sharing_service import SharingError, accept_share as accept

    try:
        grant = accept(request.user, pk)
    except SharingError as exc:
        return Response({'error': exc.message}, status=status.HTTP_404_NOT_FOUND)
    return Response(SharingGrantSerializer(grant).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def decline_share(request, pk):
    from apps.accounts.sharing_service import SharingError, decline_share as decline

    try:
        decline(request.user, pk)
    except SharingError as exc:
        return Response({'error': exc.message}, status=status.HTTP_404_NOT_FOUND)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def revoke_share(request, pk):
    """Stop sharing. Takes effect on the recipient's next request."""
    from apps.accounts.models import SharingGrant
    from apps.accounts.sharing_service import SharingError, revoke_share as revoke

    grant = get_object_or_404(SharingGrant, pk=pk)
    try:
        revoke(request.user, grant, request.data.get('reason') or '')
    except SharingError:
        return Response({'error': 'Not permitted.'}, status=status.HTTP_403_FORBIDDEN)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def shared_patient_detail(request, pk):
    """
    What the caller may actually see of someone who shared with them.

    Mirrors accounts.views.shared_patient exactly, including 404 (never 403)
    when no grant exists at all, so this endpoint cannot be used to confirm a
    subject id exists to someone with zero access.
    """
    try:
        from apps.accounts.authz import sharing_grant
        from apps.accounts.models import CustomUser, DoctorAccessLog
        from apps.ai_insights.models import HealthAlert
        from apps.appointments.models import Appointment
        from apps.medical_records.models import MedicalRecord

        try:
            subject = CustomUser.objects.get(pk=pk)
        except CustomUser.DoesNotExist:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        grant = None
        for scope in ('records', 'alerts', 'appointments'):
            grant = sharing_grant(request.user, subject, scope)
            if grant is not None:
                break
        if grant is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        records = alerts = appointments = None
        care_signals = care_activity = None

        if grant.allows('records'):
            qs = MedicalRecord.objects.filter(patient=subject)
            # A frozen share shows the record as it stood, not as it grows.
            if grant.data_cutoff is not None:
                qs = qs.filter(uploaded_at__lt=grant.data_cutoff)
            records = MedicalRecordSerializer(
                qs.order_by('-record_date', '-uploaded_at')[:100], many=True).data

        if grant.allows('alerts'):
            alerts = HealthAlertSerializer(
                HealthAlert.objects.filter(patient=subject).order_by('-created_at')[:20],
                many=True).data

            # Care monitoring belongs to the SAME scope, shown alongside alerts
            # exactly as accounts.views.shared_patient does.
            from apps.care.models import MonitoringSignal

            signals_qs = (MonitoringSignal.objects
                          .filter(patient=subject, resolved_at__isnull=True)
                          .prefetch_related('occurrences', 'reports')
                          .order_by('-created_at')[:20])
            care_signals = MonitoringSignalSerializer(signals_qs, many=True).data
            care_activity = _care_activity(subject)

        if grant.allows('appointments'):
            appointments = AppointmentSerializer(
                Appointment.objects.filter(patient=subject, is_cancelled=False)
                .order_by('appointment_datetime')[:20], many=True).data

        # Reading someone else's record is an access event, recorded in THEIR
        # trail exactly as a clinician's read is.
        DoctorAccessLog.objects.create(
            actor=request.user, patient=subject, resource='shared:patient_overview')

        return Response({
            'subject':       UserSerializer(subject).data,
            'is_effective':  grant.is_effective,
            'records':       records,
            'alerts':        alerts,
            'appointments':  appointments,
            'care_signals':  care_signals,
            'care_activity': care_activity,
        })
    except Exception as exc:
        _log.exception('shared_patient_detail error: %s', exc)
        return Response(client_error(exc, context='sharing'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _care_activity(subject):
    """
    A week of care answers, counted.

    Mirrors accounts.views._care_activity exactly. The four states are shown
    separately and never summed into an adherence figure: 'unconfirmed' is
    the app not hearing back, 'missed' is the person saying they missed it,
    and averaging those into one percentage would turn our ignorance into
    their behaviour.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.care.models import TaskOccurrence

    since = timezone.now() - timedelta(days=7)
    occurrences = TaskOccurrence.objects.filter(patient=subject, due_at__gte=since)
    return {
        'confirmed':   occurrences.filter(state=TaskOccurrence.State.CONFIRMED).count(),
        'unconfirmed': occurrences.filter(state=TaskOccurrence.State.UNCONFIRMED).count(),
        'missed':      occurrences.filter(state=TaskOccurrence.State.MISSED).count(),
        'skipped':     occurrences.filter(state=TaskOccurrence.State.SKIPPED).count(),
    }

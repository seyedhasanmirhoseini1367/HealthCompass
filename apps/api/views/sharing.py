import logging

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..serializers import (AppointmentSerializer, HealthAlertSerializer,
                           MedicalRecordSerializer, MonitoringSignalSerializer,
                           SharingGrantSerializer, UserSerializer)
from healthcompass.errors import client_error

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
def create_share(request):
    """Share part of your record with someone. The grantor is always request.user."""
    try:
        from apps.accounts.authz import can_create_grant
        from apps.accounts.models import CustomUser, DoctorAccessLog, SharingGrant

        identifier = (request.data.get('identifier') or '').strip().lower()
        if not identifier:
            return Response({'error': 'Enter the username or email of the person to share with.'},
                            status=status.HTTP_400_BAD_REQUEST)

        recipient = (CustomUser.objects
                     .filter(Q(username__iexact=identifier) | Q(email__iexact=identifier))
                     .first())
        if recipient is None:
            # Same message whether or not the account exists, so this endpoint
            # cannot be used to confirm which emails are registered.
            return Response({'error': 'No account matches that username or email.'},
                            status=status.HTTP_400_BAD_REQUEST)

        if recipient.pk == request.user.pk:
            return Response({'error': 'You already have access to your own records.'},
                            status=status.HTTP_400_BAD_REQUEST)

        if not can_create_grant(request.user, request.user):
            return Response({'error': 'Not permitted.'}, status=status.HTTP_403_FORBIDDEN)

        scopes = request.data.get('scopes') or []
        if not scopes:
            return Response({'error': 'Choose at least one thing to share.'},
                            status=status.HTTP_400_BAD_REQUEST)

        grant, created = SharingGrant.objects.get_or_create(
            patient=request.user, recipient=recipient,
            defaults={'can_view_records':      'records' in scopes,
                      'can_view_alerts':        'alerts' in scopes,
                      'can_view_appointments':  'appointments' in scopes})

        if not created:
            # Re-sharing updates the scopes and un-revokes, rather than failing
            # on the uniqueness constraint — an explicit act either way.
            grant.can_view_records = 'records' in scopes
            grant.can_view_alerts = 'alerts' in scopes
            grant.can_view_appointments = 'appointments' in scopes
            grant.status = SharingGrant.Status.ACTIVE
            grant.revoked_at = None
            grant.revoked_by = None
            grant.revoke_reason = ''
            grant.save()

        DoctorAccessLog.objects.create(
            actor=request.user, patient=request.user,
            resource=f'share_granted:{recipient.pk}')

        return Response(SharingGrantSerializer(grant).data, status=status.HTTP_201_CREATED)
    except Exception as exc:
        _log.exception('create_share error: %s', exc)
        return Response(client_error(exc, context='sharing'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def revoke_share(request, pk):
    """Stop sharing. Takes effect on the recipient's next request."""
    from apps.accounts.authz import can_revoke_grant
    from apps.accounts.models import DoctorAccessLog, SharingGrant

    grant = get_object_or_404(SharingGrant, pk=pk)
    if not can_revoke_grant(request.user, grant):
        return Response({'error': 'Not permitted.'}, status=status.HTTP_403_FORBIDDEN)

    grant.revoke(by=request.user, reason=(request.data.get('reason') or ''))

    DoctorAccessLog.objects.create(
        actor=request.user, patient=grant.patient,
        resource=f'share_revoked:{grant.recipient_id}')

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

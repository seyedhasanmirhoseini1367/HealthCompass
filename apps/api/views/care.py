import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..serializers import CareTaskSerializer, PatientReportSerializer, TaskOccurrenceSerializer
from healthcompass.errors import client_error

_log = logging.getLogger(__name__)


def _looks_like_time(value: str) -> bool:
    """HH:MM, 24-hour. Rejected rather than coerced — 25:00 is a typo, not 01:00.

    Mirrors apps.care.views._looks_like_time exactly; kept as a small local
    copy rather than importing a private view-module helper into the API.
    """
    parts = str(value).split(':')
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def care_tasks_list_create(request):
    try:
        from apps.care.models import CareTask
        from apps.care.scheduling import generate_occurrences

        if request.method == 'GET':
            qs = CareTask.objects.filter(patient=request.user, is_active=True)
            return Response(CareTaskSerializer(qs, many=True).data)

        label = (request.data.get('label') or '').strip()
        if not label:
            return Response({'error': 'Please give the reminder a name.'},
                            status=status.HTTP_400_BAD_REQUEST)

        times = request.data.get('times_of_day') or []
        valid = [t for t in times if _looks_like_time(t)]
        if not valid:
            return Response({'error': 'Please give at least one time, like 08:00.'},
                            status=status.HTTP_400_BAD_REQUEST)

        kind = request.data.get('kind') or CareTask.Kind.MEDICATION
        if kind not in CareTask.Kind.values:
            kind = CareTask.Kind.MEDICATION

        task = CareTask.objects.create(
            patient=request.user, label=label[:120], kind=kind, times_of_day=valid)

        # Materialise straight away rather than waiting for the next scheduled
        # run — a patient who has just set up an 08:00 reminder should see it.
        generate_occurrences(patient=request.user)

        return Response(CareTaskSerializer(task).data, status=status.HTTP_201_CREATED)
    except Exception as exc:
        _log.exception('care_tasks_list_create error: %s', exc)
        return Response(client_error(exc, context='care'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def care_task_stop(request, pk):
    """Stop a reminder without erasing what it already recorded."""
    from django.utils import timezone

    from apps.care.models import CareTask, TaskOccurrence

    task = get_object_or_404(CareTask, pk=pk, patient=request.user)
    task.is_active = False
    task.save(update_fields=['is_active', 'updated_at'])

    # Future occurrences are removed; past ones stay as evidence.
    TaskOccurrence.objects.filter(
        task=task, due_at__gt=timezone.now(),
        state=TaskOccurrence.State.PENDING).delete()

    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def care_occurrences_list(request):
    try:
        from datetime import timedelta

        from django.utils import timezone

        from apps.care.models import TaskOccurrence

        window = request.query_params.get('window', 'today')
        now    = timezone.now()
        span   = timedelta(days=7) if window == 'week' else timedelta(days=1)

        qs = (TaskOccurrence.objects
              .filter(patient=request.user,
                      due_at__gte=now - span, due_at__lte=now + span)
              .select_related('task')
              .order_by('-due_at'))
        return Response(TaskOccurrenceSerializer(qs, many=True).data)
    except Exception as exc:
        _log.exception('care_occurrences_list error: %s', exc)
        return Response(client_error(exc, context='care'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def occurrence_respond(request, pk):
    """
    Record the patient's answer to one occurrence.

    Ownership is the filter, not a check after the fact — an occurrence
    belonging to someone else is not found rather than found-and-refused.
    """
    try:
        from apps.care.models import PatientReport, TaskOccurrence

        occurrence = get_object_or_404(TaskOccurrence, pk=pk, patient=request.user)
        state = (request.data.get('state') or '').strip()

        try:
            occurrence.resolve(state, by=request.user,
                               input_method=PatientReport.InputMethod.API)
        except ValueError:
            return Response({'error': 'That is not a valid response.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # A reported miss is a stronger fact than silence and is acted on at
        # once, same as the web view.
        if occurrence.state == TaskOccurrence.State.MISSED:
            _raise_and_dispatch(lambda: _missed_signal(occurrence))

        return Response(TaskOccurrenceSerializer(occurrence).data)
    except Exception as exc:
        _log.exception('occurrence_respond error: %s', exc)
        return Response(client_error(exc, context='care'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def patient_reports_list_create(request):
    try:
        from apps.care.models import PatientReport

        if request.method == 'GET':
            qs = PatientReport.objects.filter(patient=request.user)[:50]
            return Response(PatientReportSerializer(qs, many=True).data)

        text = (request.data.get('text') or '').strip()
        if not text:
            return Response({'error': 'Please say a few words first.'},
                            status=status.HTTP_400_BAD_REQUEST)

        kind = request.data.get('kind') or PatientReport.Kind.SYMPTOM
        if kind not in PatientReport.Kind.values:
            kind = PatientReport.Kind.SYMPTOM

        report = PatientReport.objects.create(
            patient=request.user,
            reported_by=request.user,
            reported_by_role=PatientReport.Reporter.PATIENT,
            kind=kind,
            input_method=PatientReport.InputMethod.API,
            text=text[:4000],
        )
        _raise_and_dispatch(lambda: _report_signal(report))

        return Response(PatientReportSerializer(report).data, status=status.HTTP_201_CREATED)
    except Exception as exc:
        _log.exception('patient_reports_list_create error: %s', exc)
        return Response(client_error(exc, context='care'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Wiring (mirrors apps.care.views) ────────────────────────────────────────

def _missed_signal(occurrence):
    from apps.care.signals_rules import signal_for_missed
    return signal_for_missed(occurrence)


def _report_signal(entry):
    from apps.care.signals_rules import signal_for_report
    return signal_for_report(entry)


def _raise_and_dispatch(make_signals):
    """
    Raise signals and notify, without letting either break the patient's action.

    The patient's action is already saved; a notification pipeline failure
    must not undo that.
    """
    from apps.notifications.dispatch import dispatch_signal

    try:
        for signal in make_signals() or []:
            dispatch_signal(signal)
    except Exception:
        _log.exception('Care signal dispatch failed after a patient action')

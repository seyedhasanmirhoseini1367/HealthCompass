import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..serializers import AppointmentSerializer

_log = logging.getLogger(__name__)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def appointments_list_create(request):
    try:
        from apps.appointments.models import Appointment
        from django.utils import timezone

        if request.method == 'GET':
            qs   = Appointment.objects.filter(patient=request.user)
            show = request.query_params.get('show', 'upcoming')
            now  = timezone.now()
            if show == 'past':
                qs = qs.filter(appointment_datetime__lt=now).order_by('-appointment_datetime')
            elif show == 'all':
                qs = qs.order_by('appointment_datetime')
            else:
                qs = qs.filter(appointment_datetime__gte=now, is_cancelled=False)
            return Response(AppointmentSerializer(qs, many=True).data)

        ser = AppointmentSerializer(data=request.data)
        if ser.is_valid():
            appt = ser.save(patient=request.user)
            return Response(AppointmentSerializer(appt).data, status=status.HTTP_201_CREATED)
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:
        _log.exception('appointments_list_create error: %s', exc)
        return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def appointment_detail(request, pk):
    from apps.appointments.models import Appointment
    try:
        appt = Appointment.objects.get(pk=pk, patient=request.user)
    except Appointment.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(AppointmentSerializer(appt).data)

    if request.method == 'PATCH':
        ser = AppointmentSerializer(appt, data=request.data, partial=True)
        if ser.is_valid():
            appt = ser.save(
                reminded_24h=False, reminded_3h=False,
                reminded_2h=False,  reminded_1h=False,
            )
            return Response(AppointmentSerializer(appt).data)
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

    appt.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)

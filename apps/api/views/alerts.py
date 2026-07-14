from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..serializers import HealthAlertSerializer, NotificationSerializer


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def alerts_list(request):
    from apps.ai_insights.models import HealthAlert
    qs = HealthAlert.objects.filter(patient=request.user).order_by('-created_at')[:30]
    return Response(HealthAlertSerializer(qs, many=True).data)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def alert_mark_read(request, pk):
    from apps.ai_insights.models import HealthAlert
    try:
        alert = HealthAlert.objects.get(pk=pk, patient=request.user)
    except HealthAlert.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    alert.is_read = True
    alert.save(update_fields=['is_read'])
    return Response({'ok': True})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def notifications_list(request):
    from apps.notifications.models import Notification
    qs = Notification.objects.filter(user=request.user).order_by('-created_at')[:30]
    return Response(NotificationSerializer(qs, many=True).data)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def notification_mark_read(request, pk):
    from apps.notifications.models import Notification
    try:
        notif = Notification.objects.get(pk=pk, user=request.user)
    except Notification.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    notif.is_read = True
    notif.save(update_fields=['is_read'])
    return Response({'ok': True})

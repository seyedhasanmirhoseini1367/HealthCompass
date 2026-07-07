from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.medical_records.models import MedicalRecord
from .serializers import UserSerializer, RegisterSerializer, MedicalRecordSerializer


# ── Auth ──────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([AllowAny])
def register(request):
    serializer = RegisterSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response({
            'user':    UserSerializer(user).data,
            'access':  str(refresh.access_token),
            'refresh': str(refresh),
        }, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(UserSerializer(request.user).data)


# ── Medical Records ───────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def records_list(request):
    qs = MedicalRecord.objects.filter(patient=request.user).order_by('-uploaded_at')
    record_type = request.query_params.get('type')
    if record_type:
        qs = qs.filter(record_type=record_type)
    return Response(MedicalRecordSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def record_detail(request, pk):
    try:
        record = MedicalRecord.objects.get(pk=pk, patient=request.user)
    except MedicalRecord.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(MedicalRecordSerializer(record).data)


# ── Dashboard Summary ─────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_summary(request):
    user    = request.user
    records = MedicalRecord.objects.filter(patient=user)

    by_type = {}
    for rt, label in MedicalRecord.RecordType.choices:
        count = records.filter(record_type=rt).count()
        if count:
            by_type[label] = count

    return Response({
        'total_records': records.count(),
        'flagged_count': records.filter(is_flagged=True).count(),
        'records_by_type': by_type,
        'user': UserSerializer(user).data,
    })


# ── RAG Assistant ─────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def assistant_ask(request):
    query = request.data.get('query', '').strip()
    if not query:
        return Response({'error': 'query is required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        from rag_assistant.services.rag_service import RAGService
        result = RAGService().ask(request.user, query, request.data.get('history', []))
        return Response({'answer': result.get('answer', ''), 'sources': result.get('sources', [])})
    except Exception as exc:
        return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

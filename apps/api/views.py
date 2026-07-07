from django.contrib.auth import authenticate, get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.medical_records.models import MedicalRecord
from .serializers import UserSerializer, RegisterSerializer, MedicalRecordSerializer

User = get_user_model()


# ── Auth ──────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    """Accept email (or username) + password, return JWT tokens."""
    identifier = request.data.get('email', '').strip()
    password   = request.data.get('password', '')

    if not identifier or not password:
        return Response({'error': 'Email and password are required.'},
                        status=status.HTTP_400_BAD_REQUEST)

    # Look up by email first, then by username
    user = (User.objects.filter(email__iexact=identifier).first() or
            User.objects.filter(username__iexact=identifier).first())

    if not user:
        return Response({'error': 'Invalid email or password.'},
                        status=status.HTTP_401_UNAUTHORIZED)

    auth_user = authenticate(username=user.username, password=password)
    if not auth_user:
        return Response({'error': 'Invalid email or password.'},
                        status=status.HTTP_401_UNAUTHORIZED)

    if not auth_user.is_active:
        return Response({'error': 'Account is inactive.'},
                        status=status.HTTP_403_FORBIDDEN)

    refresh = RefreshToken.for_user(auth_user)
    return Response({
        'access':  str(refresh.access_token),
        'refresh': str(refresh),
        'user':    UserSerializer(auth_user).data,
    })

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


@api_view(['POST'])
@permission_classes([AllowAny])
def forgot_password(request):
    email = request.data.get('email', '').strip().lower()
    if not email:
        return Response({'error': 'Email is required.'}, status=status.HTTP_400_BAD_REQUEST)
    from django.contrib.auth import get_user_model
    from django.contrib.auth.forms import PasswordResetForm
    User = get_user_model()
    if User.objects.filter(email__iexact=email).exists():
        try:
            form = PasswordResetForm({'email': email})
            if form.is_valid():
                form.save(request=request, use_https=True,
                          email_template_name='accounts/email/password_reset_email.txt',
                          subject_template_name='accounts/email/password_reset_subject.txt')
        except Exception:
            pass  # fail silently — never reveal whether email exists
    return Response({'detail': 'If that email is registered, a reset link has been sent.'})


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

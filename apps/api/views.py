from django.contrib.auth import authenticate, get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.medical_records.models import MedicalRecord
from .serializers import (UserSerializer, RegisterSerializer, MedicalRecordSerializer,
                           MedicalRecordUploadSerializer, HealthAlertSerializer,
                           ModelPredictionSerializer, AIModelListSerializer,
                           NotificationSerializer)

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
    q = request.query_params.get('q', '').strip()
    if q:
        qs = qs.filter(title__icontains=q)
    return Response(MedicalRecordSerializer(qs, many=True).data)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def record_delete(request, pk):
    try:
        record = MedicalRecord.objects.get(pk=pk, patient=request.user)
    except MedicalRecord.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    record.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


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
    from apps.ai_insights.models import ModelPrediction, HealthAlert

    user    = request.user
    records = MedicalRecord.objects.filter(patient=user)

    by_type = {}
    for rt, label in MedicalRecord.RecordType.choices:
        count = records.filter(record_type=rt).count()
        if count:
            by_type[label] = count

    recent_alerts = HealthAlert.objects.filter(
        patient=user, is_read=False
    ).order_by('-created_at')[:5]

    recent_predictions = ModelPrediction.objects.filter(
        patient=user
    ).order_by('-created_at')[:3]

    latest_pred = ModelPrediction.objects.filter(
        patient=user, risk_score__isnull=False
    ).order_by('-created_at').first()

    return Response({
        'total_records':      records.count(),
        'flagged_count':      records.filter(is_flagged=True).count(),
        'unread_alerts':      HealthAlert.objects.filter(patient=user, is_read=False).count(),
        'records_by_type':    by_type,
        'user':               UserSerializer(user).data,
        'recent_alerts':      HealthAlertSerializer(recent_alerts, many=True).data,
        'recent_predictions': ModelPredictionSerializer(recent_predictions, many=True).data,
        'latest_risk':        round(float(latest_pred.risk_score) * 100, 1) if latest_pred else None,
    })


# ── Profile Update & Password ─────────────────────────────────────────────────

@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def profile_update(request):
    user = request.user
    for field in ['first_name', 'last_name', 'phone_number', 'date_of_birth']:
        if field in request.data:
            setattr(user, field, request.data[field] or '')
    user.save()
    return Response(UserSerializer(user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password(request):
    old_pw = request.data.get('old_password', '')
    new_pw = request.data.get('new_password', '')
    if not old_pw or not new_pw:
        return Response({'error': 'Both fields required.'}, status=status.HTTP_400_BAD_REQUEST)
    if not request.user.check_password(old_pw):
        return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)
    if len(new_pw) < 8:
        return Response({'error': 'New password must be at least 8 characters.'}, status=status.HTTP_400_BAD_REQUEST)
    request.user.set_password(new_pw)
    request.user.save()
    refresh = RefreshToken.for_user(request.user)
    return Response({'access': str(refresh.access_token), 'refresh': str(refresh)})


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def emergency_card(request):
    from apps.accounts.models import PatientProfile
    profile, _ = PatientProfile.objects.get_or_create(user=request.user)
    if request.method == 'PATCH':
        for field in ['blood_type', 'allergies', 'emergency_contact_name', 'emergency_contact_phone']:
            if field in request.data:
                setattr(profile, field, request.data[field] or '')
        profile.save()
    return Response({
        'full_name':               request.user.get_full_name() or request.user.username,
        'email':                   request.user.email,
        'date_of_birth':           str(request.user.date_of_birth) if request.user.date_of_birth else None,
        'phone_number':            request.user.phone_number,
        'blood_type':              profile.blood_type,
        'allergies':               profile.allergies,
        'emergency_contact_name':  profile.emergency_contact_name,
        'emergency_contact_phone': profile.emergency_contact_phone,
        'token':                   str(profile.emergency_token),
    })


# ── Record Upload ─────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def record_upload(request):
    serializer = MedicalRecordUploadSerializer(data=request.data, context={'request': request})
    if serializer.is_valid():
        record = serializer.save()
        return Response(MedicalRecordSerializer(record).data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ── Analytics ─────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def analytics(request):
    from collections import defaultdict
    from apps.ai_insights.models import ModelPrediction, HealthAlert
    from apps.medical_records.models import ParsedLabValue

    patient = request.user

    lab_qs = (ParsedLabValue.objects
              .filter(record__patient=patient)
              .select_related('record')
              .order_by('record__record_date', 'record__uploaded_at'))

    biomarker_map = defaultdict(list)
    for lv in lab_qs:
        try:
            numeric = float(lv.value)
        except (ValueError, TypeError):
            continue
        date_val = lv.record.record_date or lv.record.uploaded_at.date()
        biomarker_map[lv.parameter_name].append({
            'date':     str(date_val),
            'value':    numeric,
            'unit':     lv.unit or '',
            'abnormal': lv.is_abnormal,
            'critical': lv.is_critical,
            'ref':      lv.reference_range or '',
        })

    biomarker_latest = {name: pts[-1] for name, pts in biomarker_map.items()}
    biomarker_trends = {name: pts for name, pts in biomarker_map.items() if len(pts) >= 2}

    records = MedicalRecord.objects.filter(patient=patient)
    records_by_type = {}
    for rt, label in MedicalRecord.RecordType.choices:
        count = records.filter(record_type=rt).count()
        if count:
            records_by_type[label] = count

    alerts_qs      = HealthAlert.objects.filter(patient=patient).order_by('-created_at')[:8]
    predictions_qs = ModelPrediction.objects.filter(patient=patient).order_by('-created_at')[:5]

    latest_pred    = ModelPrediction.objects.filter(patient=patient, risk_score__isnull=False).order_by('-created_at').first()
    latest_risk    = round(float(latest_pred.risk_score) * 100, 1) if latest_pred else None
    last_record    = records.filter(record_date__isnull=False).order_by('-record_date').first()

    return Response({
        'total_records':    records.count(),
        'flagged_count':    records.filter(is_flagged=True).count(),
        'total_biomarkers': len(biomarker_map),
        'unread_alerts':    HealthAlert.objects.filter(patient=patient, is_read=False).count(),
        'latest_risk':      latest_risk,
        'last_record_date': str(last_record.record_date) if last_record else None,
        'biomarker_latest': biomarker_latest,
        'biomarker_trends': biomarker_trends,
        'alerts':           HealthAlertSerializer(alerts_qs, many=True).data,
        'predictions':      ModelPredictionSerializer(predictions_qs, many=True).data,
        'records_by_type':  records_by_type,
    })


# ── Health Alerts ─────────────────────────────────────────────────────────────

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


# ── Notifications ─────────────────────────────────────────────────────────────

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


# ── My Predictions ────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_predictions(request):
    from apps.ai_insights.models import ModelPrediction
    qs = ModelPrediction.objects.filter(patient=request.user).order_by('-created_at')
    return Response(ModelPredictionSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def prediction_detail_api(request, pk):
    from apps.ai_insights.models import ModelPrediction
    try:
        pred = ModelPrediction.objects.get(pk=pk, patient=request.user)
    except ModelPrediction.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(ModelPredictionSerializer(pred).data)


# ── AI Models ─────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ai_models_list(request):
    from apps.ai_insights.models import AIModel
    qs = AIModel.objects.filter(status='active').order_by('-run_count', '-created_at')
    return Response(AIModelListSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ai_model_detail(request, slug):
    from apps.ai_insights.models import AIModel
    try:
        model = AIModel.objects.get(slug=slug, status='active')
    except AIModel.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    data = AIModelListSerializer(model).data
    data['input_schema'] = model.input_schema
    data['interpretation_guide'] = model.interpretation_guide
    return Response(data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def run_model_prediction(request, slug):
    from apps.ai_insights.models import AIModel, ModelPrediction
    from apps.ai_insights.views import _sanitize
    from apps.ai_insights.runner import run_model, generate_interpretation
    try:
        model = AIModel.objects.get(slug=slug, status='active')
    except AIModel.DoesNotExist:
        return Response({'error': 'Model not found.'}, status=status.HTTP_404_NOT_FOUND)

    if model.input_type not in ('tabular',):
        return Response(
            {'error': 'This model requires file upload. Please use the website to run it.'},
            status=status.HTTP_400_BAD_REQUEST)

    input_data = {k: request.data.get(k, '') for k in model.input_schema.keys()}
    try:
        result = _sanitize(run_model(model, input_data, None))
        if not result.get('success'):
            raise ValueError(result.get('error', 'Prediction failed'))
        interpretation = generate_interpretation(model, result, input_data)
        pred = ModelPrediction.objects.create(
            model=model, patient=request.user,
            input_data=input_data, result=result,
            risk_score=result.get('risk_score'),
            interpretation=interpretation,
        )
        AIModel.objects.filter(pk=model.pk).update(run_count=model.run_count + 1)
        return Response(ModelPredictionSerializer(pred).data, status=status.HTTP_201_CREATED)
    except Exception as exc:
        return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Seizure Analysis Proxy ────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def seizure_analysis(request):
    """Proxy EEG parquet/CSV file to hasanai.net seizure comparison API."""
    import requests as http_requests
    from apps.ai_insights.models import AIModel, ModelPrediction
    from django.conf import settings
    from django.contrib.auth import get_user_model
    import logging
    logger = logging.getLogger(__name__)

    uploaded_file = request.FILES.get('signal_file')
    if not uploaded_file:
        return Response({'error': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        resp = http_requests.post(
            'https://hasanai.net/seizure-comparison/predict/',
            files={'signal_file': (uploaded_file.name, uploaded_file.read(), uploaded_file.content_type)},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
    except http_requests.Timeout:
        return Response({'error': 'Analysis timed out. The file may be too large or the server is busy.'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
    except Exception as exc:
        logger.exception('seizure_analysis proxy error: %s', exc)
        return Response({'error': 'Could not reach the analysis server. Please try again.'}, status=status.HTTP_502_BAD_GATEWAY)

    try:
        User = get_user_model()
        admin_user = User.objects.filter(is_staff=True).first() or request.user
        ai_model, _ = AIModel.objects.get_or_create(
            slug='eeg-seizure-detection',
            defaults={
                'name': 'EEG Seizure Detection',
                'description': 'Ensemble seizure detection via hasanai.net external API.',
                'category': AIModel.Category.NEUROLOGY,
                'input_type': AIModel.InputType.PARQUET,
                'status': AIModel.Status.ACTIVE,
                'data_scientist': admin_user,
            },
        )
        label      = data.get('ensemble_label', '')
        confidence = data.get('ensemble_confidence') or data.get('confidence')
        if confidence is not None:
            try: confidence = float(confidence)
            except (TypeError, ValueError): confidence = None
        risk_score = None
        if confidence is not None:
            risk_score = confidence if 'seizure' in label.lower() else (1 - confidence)
        pred = ModelPrediction.objects.create(
            model=ai_model,
            patient=request.user,
            input_data={'filename': uploaded_file.name},
            result=data,
            risk_score=risk_score,
        )
        AIModel.objects.filter(pk=ai_model.pk).update(run_count=ai_model.run_count + 1)
        data['prediction_id'] = str(pred.pk)
    except Exception as save_err:
        logger.warning('Could not save seizure prediction: %s', save_err)

    return Response(data)


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

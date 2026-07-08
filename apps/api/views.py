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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def register_fcm_token(request):
    token = request.data.get('token', '').strip()
    if not token:
        return Response({'error': 'Token required.'}, status=status.HTTP_400_BAD_REQUEST)
    from apps.notifications.models import FCMDevice
    FCMDevice.objects.update_or_create(token=token, defaults={'user': request.user})
    return Response({'detail': 'Token registered.'})


# ── Medical Records ───────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def records_list(request):
    from django.db.models import Q
    qs = MedicalRecord.objects.filter(patient=request.user).order_by('-record_date', '-uploaded_at')
    record_type = request.query_params.get('type')
    if record_type:
        qs = qs.filter(record_type=record_type)
    q = request.query_params.get('q', '').strip()
    if q:
        qs = qs.filter(title__icontains=q)
    date_from = request.query_params.get('date_from', '').strip()
    date_to   = request.query_params.get('date_to',   '').strip()
    if date_from:
        qs = qs.filter(Q(record_date__gte=date_from) | Q(record_date__isnull=True, uploaded_at__date__gte=date_from))
    if date_to:
        qs = qs.filter(Q(record_date__lte=date_to)   | Q(record_date__isnull=True, uploaded_at__date__lte=date_to))
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
    from apps.api.serializers import MedicalRecordDetailSerializer
    return Response(MedicalRecordDetailSerializer(record).data)


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
def profile_picture_upload(request):
    pic = request.FILES.get('profile_picture')
    if not pic:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)
    allowed = ('image/jpeg', 'image/png', 'image/webp', 'image/gif')
    if pic.content_type not in allowed:
        return Response({'error': 'Only JPEG, PNG, WebP, or GIF images are accepted.'},
                        status=status.HTTP_400_BAD_REQUEST)
    user = request.user
    if user.profile_picture:
        try: user.profile_picture.delete(save=False)
        except Exception: pass
    user.profile_picture = pic
    user.save(update_fields=['profile_picture'])
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


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_pdf_api(request):
    from apps.medical_records.parsers import PDFParser
    from apps.medical_records.models import ParsedLabValue
    from apps.medical_records.views import _create_alert
    from datetime import datetime as dt
    pdf_file = request.FILES.get('pdf_file') or request.FILES.get('file')
    if not pdf_file:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)
    pdf_bytes = pdf_file.read()
    parsed    = PDFParser().parse(pdf_bytes, use_ai=True)
    if parsed.get('error'):
        return Response({'error': parsed['error']}, status=status.HTTP_400_BAD_REQUEST)
    structured = parsed.get('structured') or {}
    rtype = structured.get('record_type') or request.data.get('record_type', MedicalRecord.RecordType.OTHER)
    title = structured.get('title') or pdf_file.name.replace('.pdf', '').replace('.PDF', '')
    rec_date = None
    if structured.get('date'):
        try: rec_date = dt.strptime(structured['date'], '%Y-%m-%d').date()
        except ValueError: pass
    record = MedicalRecord.objects.create(
        patient=request.user, record_type=rtype,
        source=MedicalRecord.Source.MANUAL_UPLOAD,
        title=title, file=pdf_file,
        raw_text=parsed.get('raw_text', ''), parsed_data=structured,
        notes=request.data.get('notes', ''), record_date=rec_date,
    )
    flagged = 0
    for lv in structured.get('lab_values', []):
        is_ab = lv.get('is_abnormal', False)
        ParsedLabValue.objects.create(
            record=record, parameter_name=lv.get('name', 'Unknown'),
            value=str(lv.get('value', '')), unit=lv.get('unit', ''),
            reference_range=lv.get('ref_range', ''), is_abnormal=is_ab,
        )
        if is_ab: flagged += 1
    if flagged:
        record.is_flagged = True; record.save(update_fields=['is_flagged'])
        _create_alert(record, flagged)
    return Response({**MedicalRecordSerializer(record).data, 'flagged': flagged}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_text_api(request):
    from apps.medical_records.parsers import TextParser
    from apps.medical_records.models import ParsedLabValue
    from apps.medical_records.views import _create_alert
    from datetime import datetime as dt
    raw_text = (request.data.get('text') or '').strip()
    if not raw_text:
        return Response({'error': 'Text is required.'}, status=status.HTTP_400_BAD_REQUEST)
    parsed     = TextParser().parse(raw_text)
    structured = parsed.get('structured') or {}
    chosen_type = request.data.get('record_type', 'auto')
    rtype = structured.get('record_type', MedicalRecord.RecordType.OTHER) if chosen_type == 'auto' else chosen_type
    title = structured.get('title') or raw_text[:60].strip().replace('\n', ' ')
    rec_date = None
    if structured.get('date'):
        try: rec_date = dt.strptime(structured['date'], '%Y-%m-%d').date()
        except ValueError: pass
    record = MedicalRecord.objects.create(
        patient=request.user, record_type=rtype,
        source=MedicalRecord.Source.MANUAL_UPLOAD,
        title=title, raw_text=raw_text, parsed_data=structured,
        notes=request.data.get('notes', ''), record_date=rec_date,
    )
    flagged = 0
    for lv in structured.get('lab_values', []):
        is_ab = lv.get('is_abnormal', False)
        ParsedLabValue.objects.create(
            record=record, parameter_name=lv.get('name', 'Unknown'),
            value=str(lv.get('value', '')), unit=lv.get('unit', ''),
            reference_range=lv.get('ref_range', ''), is_abnormal=is_ab,
        )
        if is_ab: flagged += 1
    if flagged:
        record.is_flagged = True; record.save(update_fields=['is_flagged'])
        _create_alert(record, flagged)
    try:
        from apps.rag_assistant.services.rag_service import RAGService
        RAGService().index_record(record)
    except Exception: pass
    return Response({**MedicalRecordSerializer(record).data, 'flagged': flagged}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_kanta_api(request):
    from apps.medical_records.parsers import KantaXMLParser
    from apps.medical_records.models import ParsedLabValue
    from apps.medical_records.views import _map_doc_type, _check_critical, _create_alert
    from datetime import datetime as dt
    xml_file = request.FILES.get('xml_file') or request.FILES.get('file')
    if not xml_file:
        return Response({'error': 'No XML file provided.'}, status=status.HTTP_400_BAD_REQUEST)
    parsed = KantaXMLParser().parse(xml_file.read())
    if 'error' in parsed:
        return Response({'error': parsed['error']}, status=status.HTTP_400_BAD_REQUEST)
    records_created = []; lab_values_created = 0
    for doc in parsed.get('records', []):
        rtype = _map_doc_type(doc.get('type', ''))
        rec_date = None
        if doc.get('date'):
            try: rec_date = dt.strptime(doc['date'], '%Y-%m-%d').date()
            except ValueError: pass
        record = MedicalRecord.objects.create(
            patient=request.user, record_type=rtype,
            source=MedicalRecord.Source.KANTA_XML,
            title=doc.get('title') or doc.get('type') or 'Kanta Record',
            parsed_data=doc, notes=request.data.get('notes', ''), record_date=rec_date,
        )
        flagged = 0
        for section in doc.get('sections', []):
            for entry in section.get('entries', []):
                if entry.get('kind') == 'lab':
                    is_critical = _check_critical(entry)
                    is_ab = entry.get('is_abnormal', False) or is_critical
                    ref_range = ''
                    if entry.get('ref_low') and entry.get('ref_high'):
                        ref_range = f"{entry['ref_low']} – {entry['ref_high']}"
                    ParsedLabValue.objects.create(
                        record=record, parameter_name=entry.get('name', 'Unknown'),
                        value=str(entry.get('value', '')), unit=entry.get('unit', ''),
                        reference_range=ref_range, is_abnormal=is_ab, is_critical=is_critical,
                    )
                    lab_values_created += 1
                    if is_ab: flagged += 1
        if flagged:
            record.is_flagged = True; record.save(update_fields=['is_flagged'])
            _create_alert(record, flagged)
        records_created.append(str(record.pk))
    return Response({
        'records_created': len(records_created), 'lab_values_created': lab_values_created,
        'record_ids': records_created,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_wearable_api(request):
    from apps.medical_records.parsers import WearableParser
    from apps.medical_records.models import WearableDataPoint
    from datetime import datetime as dt
    data_file = request.FILES.get('data_file') or request.FILES.get('file')
    if not data_file:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)
    data_bytes = data_file.read()
    try:
        parsed = WearableParser().parse(data_bytes, filename=data_file.name)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    if not parsed.get('data_points'):
        errs = parsed.get('errors', [])
        return Response({'error': errs[0] if errs else 'No data points found.'}, status=status.HTTP_400_BAD_REQUEST)
    device = parsed.get('device', 'unknown')
    record = MedicalRecord.objects.create(
        patient=request.user, record_type=MedicalRecord.RecordType.WEARABLE,
        source=MedicalRecord.Source.WEARABLE_CSV,
        title=f'{device.replace("_"," ").title()} — {data_file.name}',
        parsed_data={'device': device, 'count': parsed['count']},
        notes=request.data.get('notes', ''),
    )
    objs = []
    for dp in parsed['data_points']:
        try: recorded_at = dt.fromisoformat(dp['recorded_at'])
        except (ValueError, KeyError): continue
        objs.append(WearableDataPoint(
            record=record, metric=dp.get('metric','other'),
            value=dp['value'], unit=dp.get('unit',''), recorded_at=recorded_at,
        ))
    WearableDataPoint.objects.bulk_create(objs, batch_size=500)
    return Response({**MedicalRecordSerializer(record).data, 'data_points': len(objs)}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def scan_ocr_api(request):
    """OCR an image and return extracted text (does NOT create a record)."""
    from django.conf import settings as s
    image = request.FILES.get('image')
    if not image:
        return Response({'error': 'No image provided.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        from google import genai
        from google.genai import types
        api_key = getattr(s, 'GEMINI_API_KEY', '')
        if not api_key:
            return Response({'error': 'OCR service not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        img_bytes = image.read()
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type=image.content_type or 'image/jpeg'),
                ('Extract all text from this medical document image. '
                 'Return only the raw text content exactly as it appears, '
                 'preserving structure (line breaks, sections, tables). '
                 'Do not add commentary or explanations.'),
            ],
        )
        return Response({'text': (response.text or '').strip()})
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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


# ── Population Insights ───────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def population_insights(request):
    from collections import defaultdict
    from apps.medical_records.models import ParsedLabValue
    from apps.ai_insights.models import HealthAlert, ModelPrediction

    # ── Population biomarker averages (all patients, anonymized) ─────────────
    qs = ParsedLabValue.objects.select_related('record').values(
        'parameter_name', 'value', 'unit',
        'record__record_date', 'record__uploaded_at',
    )
    per_unit = defaultdict(lambda: defaultdict(list))
    for lv in qs:
        try:
            numeric = float(lv['value'])
        except (TypeError, ValueError):
            continue
        date_val  = lv['record__record_date'] or lv['record__uploaded_at'].date()
        month_key = str(date_val)[:7]
        key = (lv['parameter_name'], lv.get('unit') or '')
        per_unit[key][month_key].append(numeric)

    by_name = defaultdict(list)
    for (name, unit), months in per_unit.items():
        total = sum(len(v) for v in months.values())
        by_name[name].append((unit, total, months))

    pop_latest, pop_avg, pop_unit = {}, {}, {}
    for name, groups in by_name.items():
        best_unit, _, best_months = max(groups, key=lambda x: x[1])
        all_vals = [v for vs in best_months.values() for v in vs]
        sorted_months = sorted(best_months.keys())
        if sorted_months:
            last_vals = best_months[sorted_months[-1]]
            pop_latest[name] = {
                'value': round(sum(last_vals) / len(last_vals), 2),
                'unit':  best_unit,
                'count': len(all_vals),
            }
        if len(all_vals) >= 3:
            pop_avg[name] = round(sum(all_vals) / len(all_vals), 2)
        pop_unit[name] = best_unit

    # ── Risk distribution ─────────────────────────────────────────────────────
    all_scores = list(
        ModelPrediction.objects.filter(risk_score__isnull=False)
        .values_list('risk_score', flat=True)
    )
    risk_buckets = {'Low (0–30%)': 0, 'Moderate (30–70%)': 0, 'High (70–100%)': 0}
    for rs in all_scores:
        s = float(rs) * 100
        if s < 30:   risk_buckets['Low (0–30%)']      += 1
        elif s < 70: risk_buckets['Moderate (30–70%)'] += 1
        else:        risk_buckets['High (70–100%)']    += 1

    pop_avg_risk = round(sum(float(r) * 100 for r in all_scores) / len(all_scores), 1) \
        if all_scores else None

    # ── Alerts distribution (all patients) ───────────────────────────────────
    alerts_summary = {
        'critical': HealthAlert.objects.filter(severity='critical').count(),
        'warning':  HealthAlert.objects.filter(severity='warning').count(),
        'info':     HealthAlert.objects.filter(severity='info').count(),
    }

    # ── Records by type (all patients) ───────────────────────────────────────
    records_by_type = {}
    for rt, label in MedicalRecord.RecordType.choices:
        count = MedicalRecord.objects.filter(record_type=rt).count()
        if count:
            records_by_type[label] = count

    from django.contrib.auth import get_user_model
    User = get_user_model()

    return Response({
        'total_patients':    User.objects.filter(is_active=True, role='patient').count(),
        'total_biomarkers':  len(pop_latest),
        'total_predictions': len(all_scores),
        'pop_avg_risk':      pop_avg_risk,
        'pop_latest':        pop_latest,
        'pop_avg':           pop_avg,
        'pop_unit':          pop_unit,
        'risk_buckets':      risk_buckets,
        'alerts_summary':    alerts_summary,
        'records_by_type':   records_by_type,
    })


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


# ── Appointments ──────────────────────────────────────────────────────────────

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def appointments_list_create(request):
    import logging
    _log = logging.getLogger(__name__)
    try:
        from apps.appointments.models import Appointment
        from .serializers import AppointmentSerializer

        if request.method == 'GET':
            qs = Appointment.objects.filter(patient=request.user)
            show = request.query_params.get('show', 'upcoming')
            from django.utils import timezone
            now = timezone.now()
            if show == 'past':
                qs = qs.filter(appointment_datetime__lt=now).order_by('-appointment_datetime')
            elif show == 'all':
                qs = qs.order_by('appointment_datetime')
            else:
                qs = qs.filter(appointment_datetime__gte=now, is_cancelled=False)
            return Response(AppointmentSerializer(qs, many=True).data)

        # POST — create
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
    from .serializers import AppointmentSerializer
    try:
        appt = Appointment.objects.get(pk=pk, patient=request.user)
    except Appointment.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(AppointmentSerializer(appt).data)

    if request.method == 'PATCH':
        ser = AppointmentSerializer(appt, data=request.data, partial=True)
        if ser.is_valid():
            # Reset reminder-sent flags whenever appointment is edited
            appt = ser.save(
                reminded_24h=False, reminded_3h=False,
                reminded_2h=False,  reminded_1h=False,
            )
            return Response(AppointmentSerializer(appt).data)
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

    # DELETE
    appt.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ── RAG Assistant ─────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def assistant_ask(request):
    from apps.rag_assistant.models import ChatSession, QueryLog
    from apps.rag_assistant.services.rag_service import RAGService

    query      = request.data.get('query', '').strip()
    session_id = request.data.get('session_id')
    if not query:
        return Response({'error': 'query is required.'}, status=status.HTTP_400_BAD_REQUEST)

    # Get or create a persistent session
    if session_id:
        try:
            session = ChatSession.objects.get(pk=session_id, patient=request.user)
        except ChatSession.DoesNotExist:
            session = ChatSession.objects.create(patient=request.user, title=query[:60])
    else:
        session = ChatSession.objects.create(patient=request.user, title=query[:60])

    # Build history from DB (last 5 exchanges)
    history = list(
        session.messages.values('query', 'response').order_by('-created_at')[:5]
    )
    history.reverse()

    try:
        result          = RAGService().ask(request.user, query, history)
        response_text   = result[0]
        sources         = result[1]
        provider        = result[2] if len(result) > 2 else ''
        chunks_count    = result[3] if len(result) > 3 else None
        safety_routed   = result[4] if len(result) > 4 else False
        triggered_rules = result[5] if len(result) > 5 else []
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('assistant_ask error')
        return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Auto-title on first message
    if not session.messages.exists():
        session.title = query[:60]
    session.save(update_fields=['title', 'updated_at'])

    log = QueryLog.objects.create(
        session                = session,
        query                  = query,
        response               = response_text,
        sources                = sources,
        llm_provider           = provider,
        retrieved_chunks_count = chunks_count,
        safety_routed          = safety_routed,
        triggered_rules        = triggered_rules,
    )

    return Response({
        'answer':     response_text,
        'sources':    sources,
        'session_id': str(session.pk),
        'message_id': str(log.pk),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def assistant_sessions(request):
    """List the user's 20 most recent chat sessions."""
    from apps.rag_assistant.models import ChatSession
    sessions = ChatSession.objects.filter(patient=request.user)[:20]
    return Response({'sessions': [
        {
            'id':            str(s.pk),
            'title':         s.title,
            'created_at':    s.created_at.isoformat(),
            'updated_at':    s.updated_at.isoformat(),
            'message_count': s.messages.count(),
        }
        for s in sessions
    ]})


@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
def assistant_session_detail(request, session_id):
    """GET → messages in session. DELETE → remove session."""
    from apps.rag_assistant.models import ChatSession
    try:
        session = ChatSession.objects.get(pk=session_id, patient=request.user)
    except ChatSession.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'DELETE':
        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    messages = list(session.messages.values('id', 'query', 'response', 'created_at').order_by('created_at'))
    return Response({
        'id':       str(session.pk),
        'title':    session.title,
        'messages': [
            {
                'id':         str(m['id']),
                'query':      m['query'],
                'response':   m['response'],
                'created_at': m['created_at'].isoformat() if m['created_at'] else None,
            }
            for m in messages
        ],
    })

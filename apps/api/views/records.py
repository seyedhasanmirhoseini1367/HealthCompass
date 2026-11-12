from django.core.exceptions import ValidationError
from ..pagination import paginated
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.medical_records.models import MedicalRecord
from apps.medical_records.services import (ALLOWED_EXTENSIONS, MedicalRecordService, queue_pdf,
                                           queue_text, upload_kind_for, validate_upload)
from ..serializers import (MedicalRecordDetailSerializer, MedicalRecordListSerializer,
                           MedicalRecordSerializer)
from ..throttling import OCRThrottle, UploadThrottle


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def records_list(request):
    qs = MedicalRecord.objects.filter(patient=request.user).order_by('-record_date', '-uploaded_at')
    record_type = request.query_params.get('type')
    if record_type:
        qs = qs.filter(record_type=record_type)
    q = request.query_params.get('q', '').strip()
    if q:
        qs = qs.filter(title__icontains=q)
    from healthcompass.params import parse_date_param
    raw_from = request.query_params.get('date_from', '').strip()
    raw_to   = request.query_params.get('date_to',   '').strip()
    date_from, date_to = parse_date_param(raw_from), parse_date_param(raw_to)
    if (raw_from and not date_from) or (raw_to and not date_to):
        return Response({'error': 'date_from and date_to must be YYYY-MM-DD.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if date_from:
        qs = qs.filter(Q(record_date__gte=date_from) | Q(record_date__isnull=True, uploaded_at__date__gte=date_from))
    if date_to:
        qs = qs.filter(Q(record_date__lte=date_to)   | Q(record_date__isnull=True, uploaded_at__date__lte=date_to))
    # Paged, and without parsed_data: the list returned every record's full
    # extraction (audit ARCH-08). The detail endpoint still has it.
    return paginated(request, qs, MedicalRecordListSerializer)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def record_detail(request, pk):
    try:
        record = (MedicalRecord.objects.prefetch_related('lab_values__corrections')
                  .get(pk=pk, patient=request.user))
    except (MedicalRecord.DoesNotExist, ValidationError, ValueError):
        # A malformed UUID must read as 'not found', not blow up with a 500.
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(MedicalRecordDetailSerializer(record).data)


def _queued_response(result):
    """201 when processing already finished (inline), 202 while a worker has it."""
    record = result['record']
    S = MedicalRecord.ProcessingStatus
    if record.processing_status == S.FAILED and not result.get('duplicate'):
        record.delete()  # failed inline: no half-made record, as before
        return Response({'error': 'This file could not be processed.'},
                        status=status.HTTP_400_BAD_REQUEST)
    code = (status.HTTP_202_ACCEPTED if record.processing_status in (S.QUEUED, S.PROCESSING)
            else status.HTTP_201_CREATED)
    return Response({**MedicalRecordSerializer(record).data, 'flagged': result.get('flagged', 0),
                     'processing_status': record.processing_status}, status=code)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def record_status(request, pk):
    try:
        record = MedicalRecord.objects.get(pk=pk, patient=request.user)
    except (MedicalRecord.DoesNotExist, ValidationError, ValueError):
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response({'status': record.processing_status, 'error': record.processing_error})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def record_delete(request, pk):
    try:
        record = MedicalRecord.objects.get(pk=pk, patient=request.user)
    except (MedicalRecord.DoesNotExist, ValidationError, ValueError):
        # A malformed UUID must read as 'not found', not blow up with a 500.
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    record.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
def record_upload(request):
    """Generic upload: the file type is detected from its validated extension
    and routed to the same service the typed endpoints use. It used to save any
    file directly, with no extension, magic-byte or size check (audit SEC-08)."""
    upload = request.FILES.get('file')
    if not upload:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)
    kind = upload_kind_for(upload.name)
    if kind not in ('pdf', 'kanta', 'wearable'):
        accepted = ALLOWED_EXTENSIONS['pdf'] + ALLOWED_EXTENSIONS['kanta'] + ALLOWED_EXTENSIONS['wearable']
        return Response({'error': f'Unsupported file type. Accepted: {", ".join(accepted)}'},
                        status=status.HTTP_400_BAD_REQUEST)
    ok, safe_name = validate_upload(upload, ALLOWED_EXTENSIONS[kind])
    if not ok:
        return Response({'error': safe_name}, status=status.HTTP_400_BAD_REQUEST)

    notes = request.data.get('notes', '')
    if kind == 'pdf':
        return _queued_response(queue_pdf(
            request.user, upload.read(), notes=notes,
            record_type=request.data.get('record_type') or None, filename=safe_name))
    elif kind == 'kanta':
        result = MedicalRecordService.create_from_kanta(request.user, upload.read(), notes=notes)
    else:
        result = MedicalRecordService.create_from_wearable(
            request.user, upload.read(), filename=safe_name, notes=notes)
    if result.get('error'):
        return Response({'error': result['error']}, status=status.HTTP_400_BAD_REQUEST)
    if kind == 'kanta':
        return Response({'records_created': result['records_created'],
                         'lab_values_created': result['lab_values_created'],
                         'record_ids': result['record_ids']}, status=status.HTTP_201_CREATED)
    return Response(MedicalRecordSerializer(result['record']).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
def upload_pdf_api(request):
    pdf_file = request.FILES.get('pdf_file') or request.FILES.get('file')
    if not pdf_file:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ok, safe_name = validate_upload(pdf_file, ALLOWED_EXTENSIONS['pdf'])
    if not ok:
        return Response({'error': safe_name}, status=status.HTTP_400_BAD_REQUEST)

    result = queue_pdf(
        request.user, pdf_file.read(),
        notes=request.data.get('notes', ''),
        record_type=request.data.get('record_type') or None,
        filename=safe_name,
    )
    return _queued_response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
def upload_text_api(request):
    raw_text = (request.data.get('text') or '').strip()
    from django.conf import settings as _s
    if len(raw_text) > _s.MAX_PASTED_TEXT_CHARS:
        return Response({'error': f'Text is too long (limit {_s.MAX_PASTED_TEXT_CHARS} characters).'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not raw_text:
        return Response({'error': 'Text is required.'}, status=status.HTTP_400_BAD_REQUEST)

    result = queue_text(
        request.user, raw_text,
        notes=request.data.get('notes', ''),
        record_type=request.data.get('record_type', 'auto'),
    )
    return _queued_response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
def upload_kanta_api(request):
    xml_file = request.FILES.get('xml_file') or request.FILES.get('file')
    if not xml_file:
        return Response({'error': 'No XML file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ok, msg = validate_upload(xml_file, ALLOWED_EXTENSIONS['kanta'])
    if not ok:
        return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    result = MedicalRecordService.create_from_kanta(
        request.user, xml_file.read(),
        notes=request.data.get('notes', ''),
    )
    if result.get('error'):
        return Response({'error': result['error']}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        'records_created':    result['records_created'],
        'lab_values_created': result['lab_values_created'],
        'record_ids':         result['record_ids'],
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
def upload_wearable_api(request):
    data_file = request.FILES.get('data_file') or request.FILES.get('file')
    if not data_file:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ok, safe_name = validate_upload(data_file, ALLOWED_EXTENSIONS['wearable'])
    if not ok:
        return Response({'error': safe_name}, status=status.HTTP_400_BAD_REQUEST)

    result = MedicalRecordService.create_from_wearable(
        request.user, data_file.read(),
        filename=data_file.name,
        notes=request.data.get('notes', ''),
    )
    if result.get('error'):
        return Response({'error': result['error']}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {**MedicalRecordSerializer(result['record']).data, 'data_points': result['data_points']},
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([OCRThrottle])
def scan_ocr_api(request):
    image = request.FILES.get('image')
    if not image:
        return Response({'error': 'No image provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ok, msg = validate_upload(image, ALLOWED_EXTENSIONS['image'])
    if not ok:
        return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    result = MedicalRecordService.ocr_image(
        image.read(), mime_type=image.content_type or 'image/jpeg', user=request.user,
    )
    if result.get('consent_required'):
        # 403 with a machine-readable body: existing clients already handle
        # {'error': ...}; new clients can branch on consent_required.
        return Response(
            {'error': result['error'], 'consent_required': result['consent_required']},
            status=status.HTTP_403_FORBIDDEN,
        )
    if result.get('error'):
        return Response({'error': result['error']}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response({'text': result['text']})

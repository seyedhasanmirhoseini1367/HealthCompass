from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.medical_records.models import MedicalRecord
from apps.medical_records.services import MedicalRecordService, validate_upload
from ..serializers import (MedicalRecordSerializer, MedicalRecordUploadSerializer,
                            MedicalRecordDetailSerializer)


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
    date_from = request.query_params.get('date_from', '').strip()
    date_to   = request.query_params.get('date_to',   '').strip()
    if date_from:
        qs = qs.filter(Q(record_date__gte=date_from) | Q(record_date__isnull=True, uploaded_at__date__gte=date_from))
    if date_to:
        qs = qs.filter(Q(record_date__lte=date_to)   | Q(record_date__isnull=True, uploaded_at__date__lte=date_to))
    return Response(MedicalRecordSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def record_detail(request, pk):
    try:
        record = MedicalRecord.objects.get(pk=pk, patient=request.user)
    except MedicalRecord.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(MedicalRecordDetailSerializer(record).data)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def record_delete(request, pk):
    try:
        record = MedicalRecord.objects.get(pk=pk, patient=request.user)
    except MedicalRecord.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    record.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


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
    pdf_file = request.FILES.get('pdf_file') or request.FILES.get('file')
    if not pdf_file:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ok, safe_name = validate_upload(pdf_file, ['pdf'])
    if not ok:
        return Response({'error': safe_name}, status=status.HTTP_400_BAD_REQUEST)

    result = MedicalRecordService.create_from_pdf(
        request.user, pdf_file.read(),
        notes=request.data.get('notes', ''),
        record_type=request.data.get('record_type') or None,
        filename=safe_name,
    )
    if result.get('error'):
        return Response({'error': result['error']}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {**MedicalRecordSerializer(result['record']).data, 'flagged': result['flagged']},
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_text_api(request):
    raw_text = (request.data.get('text') or '').strip()
    if not raw_text:
        return Response({'error': 'Text is required.'}, status=status.HTTP_400_BAD_REQUEST)

    result = MedicalRecordService.create_from_text(
        request.user, raw_text,
        notes=request.data.get('notes', ''),
        record_type=request.data.get('record_type', 'auto'),
    )
    return Response(
        {**MedicalRecordSerializer(result['record']).data, 'flagged': result['flagged']},
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def upload_kanta_api(request):
    xml_file = request.FILES.get('xml_file') or request.FILES.get('file')
    if not xml_file:
        return Response({'error': 'No XML file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ok, msg = validate_upload(xml_file, ['xml'])
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
def upload_wearable_api(request):
    data_file = request.FILES.get('data_file') or request.FILES.get('file')
    if not data_file:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ok, safe_name = validate_upload(data_file, ['csv', 'json', 'parquet', 'xlsx'])
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
def scan_ocr_api(request):
    image = request.FILES.get('image')
    if not image:
        return Response({'error': 'No image provided.'}, status=status.HTTP_400_BAD_REQUEST)

    ok, msg = validate_upload(image, ['jpg', 'jpeg', 'png', 'gif', 'webp'])
    if not ok:
        return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    result = MedicalRecordService.ocr_image(
        image.read(), mime_type=image.content_type or 'image/jpeg'
    )
    if result.get('error'):
        return Response({'error': result['error']}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response({'text': result['text']})

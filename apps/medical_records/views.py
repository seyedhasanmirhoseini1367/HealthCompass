import logging
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from .forms import KantaUploadForm, WearableUploadForm, PDFUploadForm, TextPasteForm
from .models import MedicalRecord, ParsedLabValue, WearableDataPoint
from .parsers import KantaXMLParser, WearableParser, PDFParser, TextParser

logger = logging.getLogger(__name__)


# ─── Record List ─────────────────────────────────────────────────────────────

@login_required
def record_list(request):
    from datetime import date, timedelta

    qs = MedicalRecord.objects.filter(patient=request.user).order_by('-record_date', '-uploaded_at')

    # ── Type filter ───────────────────────────────────────────────────────────
    rtype = request.GET.get('type', '').strip()
    if rtype:
        qs = qs.filter(record_type=rtype)

    # ── Time-period filter ────────────────────────────────────────────────────
    period = request.GET.get('period', '').strip()
    today  = date.today()
    PERIOD_CUTOFFS = {
        '30d': today - timedelta(days=30),
        '3m':  today - timedelta(days=90),
        '6m':  today - timedelta(days=180),
        '1y':  today - timedelta(days=365),
    }
    date_from = request.GET.get('date_from', '').strip()
    date_to   = request.GET.get('date_to',   '').strip()

    if period in PERIOD_CUTOFFS:
        cutoff = PERIOD_CUTOFFS[period]
        # Include records whose record_date is within range, OR whose
        # record_date is NULL (date not extracted) but uploaded within range.
        qs = qs.filter(
            Q(record_date__gte=cutoff) |
            Q(record_date__isnull=True, uploaded_at__date__gte=cutoff)
        )
    elif period == 'custom':
        if date_from:
            qs = qs.filter(
                Q(record_date__gte=date_from) |
                Q(record_date__isnull=True, uploaded_at__date__gte=date_from)
            )
        if date_to:
            qs = qs.filter(
                Q(record_date__lte=date_to) |
                Q(record_date__isnull=True, uploaded_at__date__lte=date_to)
            )

    # ── Keyword search (title) ────────────────────────────────────────────────
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(title__icontains=q)

    total_count    = MedicalRecord.objects.filter(patient=request.user).count()
    flagged_count  = MedicalRecord.objects.filter(patient=request.user, is_flagged=True).count()
    filtered_count = qs.count()

    from django.core.paginator import Paginator
    paginator   = Paginator(qs, 20)
    page_number = request.GET.get('page', 1)
    page_obj    = paginator.get_page(page_number)

    return render(request, 'medical_records/list.html', {
        'records':        page_obj,
        'page_obj':       page_obj,
        'record_types':   MedicalRecord.RecordType.choices,
        'active_type':    rtype,
        'active_period':  period,
        'active_q':       q,
        'date_from':      date_from,
        'date_to':        date_to,
        'flagged_count':  flagged_count,
        'total_count':    total_count,
        'filtered_count': filtered_count,
    })


# ─── Record Detail ────────────────────────────────────────────────────────────

@login_required
def record_detail(request, pk):
    record = get_object_or_404(MedicalRecord, pk=pk, patient=request.user)
    lab_values = record.lab_values.all()
    wearable_points = record.wearable_points.all()[:200]
    return render(request, 'medical_records/detail.html', {
        'record': record,
        'lab_values': lab_values,
        'wearable_points': wearable_points,
    })


# ─── Delete Record ────────────────────────────────────────────────────────────

@login_required
def record_delete(request, pk):
    record = get_object_or_404(MedicalRecord, pk=pk, patient=request.user)
    if request.method == 'POST':
        record.delete()
        messages.success(request, 'Record deleted.')
        return redirect('medical_records:list')
    return render(request, 'medical_records/confirm_delete.html', {'record': record})


# ─── Kanta XML Upload ─────────────────────────────────────────────────────────

@login_required
def upload_kanta(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = KantaUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        xml_file = request.FILES['xml_file']
        xml_bytes = xml_file.read()

        parser = KantaXMLParser()
        parsed = parser.parse(xml_bytes)

        if 'error' in parsed:
            if is_ajax:
                return JsonResponse({'success': False, 'error': f'XML parse error: {parsed["error"]}'}, status=400)
            messages.error(request, f'XML parse error: {parsed["error"]}')
            return render(request, 'medical_records/upload_kanta.html', {'form': form})

        records_created = 0
        lab_values_created = 0
        flagged = 0

        for doc in parsed.get('records', []):
            # Determine record type from doc type string
            rtype = _map_doc_type(doc.get('type', ''))
            rec_date = None
            if doc.get('date'):
                try:
                    rec_date = datetime.strptime(doc['date'], '%Y-%m-%d').date()
                except ValueError:
                    pass

            title = doc.get('title') or doc.get('type') or 'Kanta Record'

            record = MedicalRecord.objects.create(
                patient=request.user,
                record_type=rtype,
                source=MedicalRecord.Source.KANTA_XML,
                title=title,
                parsed_data=doc,
                notes=form.cleaned_data.get('notes', ''),
                record_date=rec_date,
            )
            records_created += 1

            # Extract lab values from sections
            for section in doc.get('sections', []):
                for entry in section.get('entries', []):
                    if entry.get('kind') == 'lab':
                        is_critical = _check_critical(entry)
                        is_ab = entry.get('is_abnormal', False) or is_critical

                        ref_range = ''
                        if entry.get('ref_low') and entry.get('ref_high'):
                            ref_range = f"{entry['ref_low']} – {entry['ref_high']}"
                        elif entry.get('ref_low'):
                            ref_range = f"≥ {entry['ref_low']}"
                        elif entry.get('ref_high'):
                            ref_range = f"≤ {entry['ref_high']}"

                        measured_at = None
                        if entry.get('date'):
                            try:
                                measured_at = datetime.strptime(entry['date'], '%Y-%m-%d')
                            except ValueError:
                                pass

                        ParsedLabValue.objects.create(
                            record=record,
                            parameter_name=entry.get('name', 'Unknown'),
                            value=str(entry.get('value', '')),
                            unit=entry.get('unit', ''),
                            reference_range=ref_range,
                            is_abnormal=is_ab,
                            is_critical=is_critical,
                            measured_at=measured_at,
                        )
                        lab_values_created += 1

                        if is_ab:
                            flagged += 1

            if flagged:
                record.is_flagged = True
                record.save(update_fields=['is_flagged'])
                _create_alert(record, flagged)

        msg = f'Imported {records_created} record(s) with {lab_values_created} lab value(s).'
        if flagged:
            msg += f' ⚠️ {flagged} abnormal value(s) detected.'

        if is_ajax:
            return JsonResponse({'success': True, 'reload': True, 'message': msg, 'flagged': bool(flagged)})

        if flagged:
            messages.warning(request, msg)
        else:
            messages.success(request, msg)
        return redirect('medical_records:list')

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Upload failed. Please check your XML file.'}, status=400)
    return render(request, 'medical_records/upload_kanta.html', {'form': form})


# ─── Wearable Data Upload ─────────────────────────────────────────────────────

@login_required
def upload_wearable(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = WearableUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        data_file = request.FILES['data_file']
        data_bytes = data_file.read()

        try:
            parser = WearableParser()
            parsed = parser.parse(data_bytes, filename=data_file.name)
        except Exception as exc:
            logger.warning('Wearable parse error: %s', exc)
            err = f'Could not read file: {exc}'
            if is_ajax:
                return JsonResponse({'success': False, 'error': err}, status=400)
            messages.error(request, err)
            return render(request, 'medical_records/upload_wearable.html', {'form': form})

        if not parsed.get('data_points'):
            parse_errors = parsed.get('errors', [])
            err = ('No data points found. ' + parse_errors[0]) if parse_errors else 'No data points found. Check file format.'
            if is_ajax:
                return JsonResponse({'success': False, 'error': err}, status=400)
            messages.error(request, err)
            return render(request, 'medical_records/upload_wearable.html', {'form': form})

        device = parsed.get('device', 'unknown')
        dp_count = parsed['count']

        record = MedicalRecord.objects.create(
            patient=request.user,
            record_type=MedicalRecord.RecordType.WEARABLE,
            source=MedicalRecord.Source.WEARABLE_CSV,
            title=f'{device.replace("_", " ").title()} — {data_file.name}',
            parsed_data={'device': device, 'count': dp_count},
            notes=form.cleaned_data.get('notes', ''),
        )

        wearable_objects = []
        for dp in parsed['data_points']:
            try:
                recorded_at = datetime.fromisoformat(dp['recorded_at'])
            except (ValueError, KeyError):
                continue
            wearable_objects.append(WearableDataPoint(
                record=record,
                metric=dp.get('metric', 'other'),
                value=dp['value'],
                unit=dp.get('unit', ''),
                recorded_at=recorded_at,
            ))

        WearableDataPoint.objects.bulk_create(wearable_objects, batch_size=500)

        msg = f'Imported {len(wearable_objects)} data point(s) from {device}.'
        if parsed.get('errors'):
            msg += f' ({len(parsed["errors"])} rows skipped)'

        if is_ajax:
            return JsonResponse({'success': True, 'record': _record_json(record), 'message': msg, 'flagged': False})

        messages.success(request, msg)
        return redirect('medical_records:list')

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Upload failed. Please check your CSV file.'}, status=400)
    return render(request, 'medical_records/upload_wearable.html', {'form': form})


# ─── PDF Upload ───────────────────────────────────────────────────────────────

@login_required
def upload_pdf(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = PDFUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        pdf_file = request.FILES['pdf_file']
        pdf_bytes = pdf_file.read()

        parser = PDFParser()
        parsed = parser.parse(pdf_bytes, use_ai=True)

        if parsed.get('error'):
            if is_ajax:
                return JsonResponse({'success': False, 'error': f'PDF parse error: {parsed["error"]}'}, status=400)
            messages.error(request, f'PDF parse error: {parsed["error"]}')
            return render(request, 'medical_records/upload_pdf.html', {'form': form})

        structured = parsed.get('structured') or {}
        rtype = structured.get('record_type') or form.cleaned_data['record_type']
        title = structured.get('title') or pdf_file.name.replace('.pdf', '')

        rec_date = None
        if structured.get('date'):
            try:
                rec_date = datetime.strptime(structured['date'], '%Y-%m-%d').date()
            except ValueError:
                pass

        record = MedicalRecord.objects.create(
            patient=request.user,
            record_type=rtype,
            source=MedicalRecord.Source.MANUAL_UPLOAD,
            title=title,
            file=pdf_file,
            raw_text=parsed.get('raw_text', ''),
            parsed_data=structured,
            notes=form.cleaned_data.get('notes', ''),
            record_date=rec_date,
        )

        # Create lab values from AI-structured data
        flagged = 0
        for lv in structured.get('lab_values', []):
            is_critical = False
            is_ab = lv.get('is_abnormal', False)
            ParsedLabValue.objects.create(
                record=record,
                parameter_name=lv.get('name', 'Unknown'),
                value=str(lv.get('value', '')),
                unit=lv.get('unit', ''),
                reference_range=lv.get('ref_range', ''),
                is_abnormal=is_ab,
                is_critical=is_critical,
            )
            if is_ab:
                flagged += 1

        if flagged:
            record.is_flagged = True
            record.save(update_fields=['is_flagged'])
            _create_alert(record, flagged)

        msg = f'Document uploaded and parsed ({parsed["page_count"]} page(s)).'
        if structured:
            msg += ' AI extracted structured data.'

        if is_ajax:
            return JsonResponse({'success': True, 'record': _record_json(record), 'message': msg, 'flagged': bool(flagged)})

        messages.success(request, msg)
        return redirect('medical_records:detail', pk=record.pk)

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Upload failed. Please check your file.'}, status=400)
    return render(request, 'medical_records/upload_pdf.html', {'form': form})


# ─── Plain Text / Paste Upload ───────────────────────────────────────────────

@login_required
def upload_text(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = TextPasteForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        raw_text = form.cleaned_data['text']
        chosen_type = form.cleaned_data['record_type']

        parser = TextParser()
        parsed = parser.parse(raw_text)
        structured = parsed.get('structured') or {}

        # Resolve record type: AI suggestion wins unless user explicitly chose
        if chosen_type == 'auto':
            rtype = structured.get('record_type', MedicalRecord.RecordType.OTHER)
        else:
            rtype = chosen_type

        title = structured.get('title') or raw_text[:60].strip().replace('\n', ' ')

        rec_date = None
        if structured.get('date'):
            try:
                rec_date = datetime.strptime(structured['date'], '%Y-%m-%d').date()
            except ValueError:
                pass

        record = MedicalRecord.objects.create(
            patient=request.user,
            record_type=rtype,
            source=MedicalRecord.Source.MANUAL_UPLOAD,
            title=title,
            raw_text=raw_text,
            parsed_data=structured,
            notes=form.cleaned_data.get('notes', ''),
            record_date=rec_date,
        )

        # Create lab values extracted by AI
        flagged = 0
        for lv in structured.get('lab_values', []):
            is_ab = lv.get('is_abnormal', False)
            ParsedLabValue.objects.create(
                record=record,
                parameter_name=lv.get('name', 'Unknown'),
                value=str(lv.get('value', '')),
                unit=lv.get('unit', ''),
                reference_range=lv.get('ref_range', ''),
                is_abnormal=is_ab,
            )
            if is_ab:
                flagged += 1

        if flagged:
            record.is_flagged = True
            record.save(update_fields=['is_flagged'])
            _create_alert(record, flagged)

        # Index into RAG so AI Assistant can answer questions about it
        try:
            from apps.rag_assistant.services.rag_service import RAGService
            RAGService().index_record(record)
        except Exception as e:
            logger.warning(f'RAG indexing failed for pasted record: {e}')

        msg = 'Record saved from pasted text.'
        if structured:
            msg += ' AI extracted structured data.'
        if flagged:
            msg += f' ⚠️ {flagged} abnormal value(s) detected.'

        if is_ajax:
            return JsonResponse({'success': True, 'record': _record_json(record), 'message': msg, 'flagged': bool(flagged)})

        if flagged:
            messages.warning(request, msg)
        else:
            messages.success(request, msg)
        return redirect('medical_records:detail', pk=record.pk)

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Please fill in all required fields.'}, status=400)
    return render(request, 'medical_records/upload_text.html', {'form': form})


# ─── Camera / OCR Scan ───────────────────────────────────────────────────────

@login_required
def scan_ocr(request):
    """AJAX: receive an image, return OCR text via Gemini Vision."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    image = request.FILES.get('image')
    if not image:
        return JsonResponse({'error': 'No image received'}, status=400)

    try:
        from django.conf import settings as s
        from google import genai
        from google.genai import types

        api_key = getattr(s, 'GEMINI_API_KEY', '')
        if not api_key:
            return JsonResponse({'error': 'Gemini API key not configured'}, status=503)

        img_bytes = image.read()
        mime_type = image.content_type or 'image/jpeg'

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type=mime_type),
                (
                    'Extract all text from this medical document image. '
                    'Return only the raw text content exactly as it appears, '
                    'preserving structure (line breaks, sections, tables). '
                    'Do not add commentary or explanations.'
                ),
            ],
        )
        return JsonResponse({'text': (response.text or '').strip()})

    except Exception as exc:
        logger.warning('Scan OCR error: %s', exc)
        return JsonResponse({'error': str(exc)}, status=500)


@login_required
def upload_scan(request):
    """Camera scan page: capture image → OCR → review text → save record."""
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    record_types = MedicalRecord.RecordType.choices

    if request.method == 'POST':
        raw_text    = request.POST.get('raw_text', '').strip()
        title       = request.POST.get('title', '').strip()
        record_type = request.POST.get('record_type', MedicalRecord.RecordType.OTHER)
        notes       = request.POST.get('notes', '').strip()
        date_str    = request.POST.get('record_date', '').strip()

        if not raw_text:
            if is_ajax:
                return JsonResponse({'success': False, 'error': 'No text provided. Please extract text from the image first.'}, status=400)
            messages.error(request, 'No text extracted. Please capture the image and extract text first.')
            return render(request, 'medical_records/upload_scan.html', {'record_types': record_types})

        parser     = TextParser()
        parsed     = parser.parse(raw_text)
        structured = parsed.get('structured') or {}

        if not title:
            title = structured.get('title') or raw_text[:60].strip().replace('\n', ' ')

        rec_date = None
        if date_str:
            try:
                rec_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        elif structured.get('date'):
            try:
                rec_date = datetime.strptime(structured['date'], '%Y-%m-%d').date()
            except ValueError:
                pass

        if record_type == 'auto':
            record_type = structured.get('record_type', MedicalRecord.RecordType.OTHER)

        record = MedicalRecord.objects.create(
            patient=request.user,
            record_type=record_type,
            source=MedicalRecord.Source.MANUAL_UPLOAD,
            title=title,
            raw_text=raw_text,
            parsed_data=structured,
            notes=notes,
            record_date=rec_date,
        )

        flagged = 0
        for lv in structured.get('lab_values', []):
            is_ab = lv.get('is_abnormal', False)
            ParsedLabValue.objects.create(
                record=record,
                parameter_name=lv.get('name', 'Unknown'),
                value=str(lv.get('value', '')),
                unit=lv.get('unit', ''),
                reference_range=lv.get('ref_range', ''),
                is_abnormal=is_ab,
            )
            if is_ab:
                flagged += 1

        if flagged:
            record.is_flagged = True
            record.save(update_fields=['is_flagged'])
            _create_alert(record, flagged)

        try:
            from apps.rag_assistant.services.rag_service import RAGService
            RAGService().index_record(record)
        except Exception as e:
            logger.warning('RAG indexing failed for scanned record: %s', e)

        msg = 'Record saved from scanned image.'
        if structured:
            msg += ' AI extracted structured data.'
        if flagged:
            msg += f' ⚠️ {flagged} abnormal value(s) detected.'

        if is_ajax:
            return JsonResponse({'success': True, 'record': _record_json(record), 'message': msg, 'flagged': bool(flagged)})

        if flagged:
            messages.warning(request, msg)
        else:
            messages.success(request, msg)
        return redirect('medical_records:detail', pk=record.pk)

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Please provide extracted text before saving.'}, status=400)
    return render(request, 'medical_records/upload_scan.html', {'record_types': record_types})


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _record_json(record):
    return {
        'pk':                  str(record.pk),
        'title':               record.title,
        'record_type':         record.record_type,
        'record_type_display': record.get_record_type_display(),
        'source_display':      record.get_source_display(),
        'record_date':         record.record_date.strftime('%b %d, %Y') if record.record_date else None,
        'uploaded_at':         record.uploaded_at.strftime('%b %d, %Y'),
        'is_flagged':          record.is_flagged,
        'detail_url':          reverse('medical_records:detail', kwargs={'pk': record.pk}),
        'delete_url':          reverse('medical_records:delete', kwargs={'pk': record.pk}),
    }


def _map_doc_type(doc_type_str: str) -> str:
    s = doc_type_str.lower()
    if any(w in s for w in ['lab', 'result', 'blood', 'urine', 'test']):
        return MedicalRecord.RecordType.LAB_RESULT
    if any(w in s for w in ['prescri', 'medication', 'drug', 'resept']):
        return MedicalRecord.RecordType.PRESCRIPTION
    if any(w in s for w in ['diagnos', 'icd']):
        return MedicalRecord.RecordType.DIAGNOSIS
    if any(w in s for w in ['vaccin', 'immuniz', 'rokote']):
        return MedicalRecord.RecordType.VACCINATION
    if any(w in s for w in ['imag', 'xray', 'mri', 'ct', 'ultrasound', 'radio']):
        return MedicalRecord.RecordType.IMAGING
    if any(w in s for w in ['discharge', 'summary', 'kotiutus']):
        return MedicalRecord.RecordType.DISCHARGE
    return MedicalRecord.RecordType.OTHER


def _check_critical(entry: dict) -> bool:
    """Very basic critical value check — in production use clinical reference ranges."""
    name = entry.get('name', '').lower()
    try:
        value = float(str(entry.get('value', '')).replace(',', '.'))
    except (ValueError, TypeError):
        return False

    critical_thresholds = {
        'hemoglobin': (value < 70 or value > 200),
        'glucose': (value < 2.5 or value > 25),
        'potassium': (value < 2.5 or value > 6.5),
        'sodium': (value < 120 or value > 160),
        'creatinine': value > 1000,
        'troponin': value > 10,
    }
    for key, is_crit in critical_thresholds.items():
        if key in name and is_crit:
            return True
    return False


def _create_alert(record: MedicalRecord, abnormal_count: int):
    """Create a HealthAlert and in-app notification for abnormal values."""
    try:
        from apps.ai_insights.models import HealthAlert
        from apps.notifications.models import Notification

        alert = HealthAlert.objects.create(
            patient=record.patient,
            source_record=record,
            severity=HealthAlert.Severity.WARNING,
            title=f'Abnormal values detected: {record.title}',
            message=(
                f'{abnormal_count} abnormal lab value(s) were found in '
                f'your uploaded record "{record.title}". Please review them.'
            ),
        )

        Notification.objects.create(
            user=record.patient,
            type=Notification.Type.HEALTH_ALERT,
            title='Abnormal values detected',
            message=f'{abnormal_count} abnormal value(s) in "{record.title}"',
            link=f'/records/{record.pk}/',
        )
    except Exception as e:
        logger.warning(f'Could not create alert: {e}')

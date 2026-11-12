import logging
from healthcompass.net import scoped_ratelimit
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils.formats import date_format
from django.views.decorators.http import require_POST
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from .forms import KantaUploadForm, WearableUploadForm, PDFUploadForm, TextPasteForm
from .models import MedicalRecord
from .services import (ALLOWED_EXTENSIONS, MedicalRecordService, accept_attr, queue_pdf,
                       queue_text, retry_processing, validate_upload)

logger = logging.getLogger(__name__)


# ─── Record List ──────────────────────────────────────────────────────────────

@login_required
def record_list(request):
    from datetime import date, timedelta

    qs = MedicalRecord.objects.filter(patient=request.user).order_by('-record_date', '-uploaded_at')

    rtype = request.GET.get('type', '').strip()
    if rtype:
        qs = qs.filter(record_type=rtype)

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
    # Malformed dates are ignored rather than handed to the ORM (a 500 before).
    from healthcompass.params import parse_date_param
    from_d, to_d = parse_date_param(date_from), parse_date_param(date_to)

    if period in PERIOD_CUTOFFS:
        cutoff = PERIOD_CUTOFFS[period]
        qs = qs.filter(
            Q(record_date__gte=cutoff) |
            Q(record_date__isnull=True, uploaded_at__date__gte=cutoff)
        )
    elif period == 'custom':
        if from_d:
            qs = qs.filter(
                Q(record_date__gte=from_d) |
                Q(record_date__isnull=True, uploaded_at__date__gte=from_d)
            )
        if to_d:
            qs = qs.filter(
                Q(record_date__lte=to_d) |
                Q(record_date__isnull=True, uploaded_at__date__lte=to_d)
            )

    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(title__icontains=q)

    total_count    = MedicalRecord.objects.filter(patient=request.user).count()
    flagged_count  = MedicalRecord.objects.filter(patient=request.user, is_flagged=True).count()
    filtered_count = qs.count()

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
        'upload_accept':  {kind: accept_attr(kind) for kind in ALLOWED_EXTENSIONS},
    })


# ─── Record Detail ────────────────────────────────────────────────────────────

@login_required
def record_detail(request, pk):
    record = get_object_or_404(MedicalRecord, pk=pk, patient=request.user)
    return render(request, 'medical_records/detail.html', {
        'record':          record,
        'lab_values':      record.lab_values.all(),
        'wearable_points': record.wearable_points.all()[:200],
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
@scoped_ratelimit('upload')
def upload_kanta(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = KantaUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        xml_file = request.FILES['xml_file']

        ok, msg = validate_upload(xml_file, ALLOWED_EXTENSIONS['kanta'])
        if not ok:
            if is_ajax:
                return JsonResponse({'success': False, 'error': msg}, status=400)
            messages.error(request, msg)
            return render(request, 'medical_records/upload_kanta.html', {'form': form})

        result = MedicalRecordService.create_from_kanta(
            request.user, xml_file.read(),
            notes=form.cleaned_data.get('notes', ''),
        )
        if result.get('error'):
            err = f'XML parse error: {result["error"]}'
            if is_ajax:
                return JsonResponse({'success': False, 'error': err}, status=400)
            messages.error(request, err)
            return render(request, 'medical_records/upload_kanta.html', {'form': form})

        summary = f'Imported {result["records_created"]} record(s) with {result["lab_values_created"]} lab value(s).'
        if result['flagged']:
            summary += f' ⚠️ {result["flagged"]} abnormal value(s) detected.'

        if is_ajax:
            return JsonResponse({'success': True, 'reload': True,
                                 'message': summary, 'flagged': bool(result['flagged'])})
        if result['flagged']:
            messages.warning(request, summary)
        else:
            messages.success(request, summary)
        return redirect('medical_records:list')

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Upload failed. Please check your XML file.'}, status=400)
    return render(request, 'medical_records/upload_kanta.html', {'form': form})


# ─── Wearable Data Upload ─────────────────────────────────────────────────────

@login_required
@scoped_ratelimit('upload')
def upload_wearable(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = WearableUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        data_file = request.FILES['data_file']

        ok, msg = validate_upload(data_file, ALLOWED_EXTENSIONS['wearable'])
        if not ok:
            if is_ajax:
                return JsonResponse({'success': False, 'error': msg}, status=400)
            messages.error(request, msg)
            return render(request, 'medical_records/upload_wearable.html', {'form': form})

        result = MedicalRecordService.create_from_wearable(
            request.user, data_file.read(),
            filename=data_file.name,
            notes=form.cleaned_data.get('notes', ''),
        )
        if result.get('error'):
            if is_ajax:
                return JsonResponse({'success': False, 'error': result['error']}, status=400)
            messages.error(request, result['error'])
            return render(request, 'medical_records/upload_wearable.html', {'form': form})

        record  = result['record']
        summary = f'Imported {result["data_points"]} data point(s) from {result["device"]}.'
        if result.get('errors'):
            summary += f' ({len(result["errors"])} rows skipped)'

        if is_ajax:
            return JsonResponse({'success': True, 'record': _record_json(record),
                                 'message': summary, 'flagged': False})
        messages.success(request, summary)
        return redirect('medical_records:list')

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Upload failed. Please check your CSV file.'}, status=400)
    return render(request, 'medical_records/upload_wearable.html', {'form': form})


# ─── PDF Upload ───────────────────────────────────────────────────────────────

@login_required
@scoped_ratelimit('upload')
def upload_pdf(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = PDFUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        pdf_file = request.FILES['pdf_file']

        ok, safe_name = validate_upload(pdf_file, ALLOWED_EXTENSIONS['pdf'])
        if not ok:
            if is_ajax:
                return JsonResponse({'success': False, 'error': safe_name}, status=400)
            messages.error(request, safe_name)
            return render(request, 'medical_records/upload_pdf.html', {'form': form})

        # Stored and answered at once; parsing runs as a job (audit ARCH-02).
        result = queue_pdf(
            request.user, pdf_file.read(),
            notes=form.cleaned_data.get('notes', ''),
            record_type=form.cleaned_data.get('record_type') or None,
            filename=safe_name,
        )
        record = result['record']
        if _failed_inline(record):
            err = 'This PDF could not be read. Please check the file.'
            if is_ajax:
                return JsonResponse({'success': False, 'error': err}, status=400)
            messages.error(request, err)
            return render(request, 'medical_records/upload_pdf.html', {'form': form})

        summary = _upload_summary(record, 'Document uploaded and parsed.')

        if is_ajax:
            return JsonResponse({'success': True, 'record': _record_json(record),
                                 'message': summary, 'flagged': bool(result['flagged'])})
        messages.success(request, summary)
        return redirect('medical_records:detail', pk=record.pk)

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Upload failed. Please check your file.'}, status=400)
    return render(request, 'medical_records/upload_pdf.html', {'form': form})


# ─── Plain Text / Paste Upload ───────────────────────────────────────────────

@login_required
@scoped_ratelimit('upload')
def upload_text(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = TextPasteForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        result = queue_text(
            request.user,
            form.cleaned_data['text'],
            notes=form.cleaned_data.get('notes', ''),
            record_type=form.cleaned_data['record_type'],
        )

        record  = result['record']
        summary = _upload_summary(record, 'Record saved from pasted text.')
        if result['flagged']:
            summary += f' ⚠️ {result["flagged"]} abnormal value(s) detected.'

        if is_ajax:
            return JsonResponse({'success': True, 'record': _record_json(record),
                                 'message': summary, 'flagged': bool(result['flagged'])})
        if result['flagged']:
            messages.warning(request, summary)
        else:
            messages.success(request, summary)
        return redirect('medical_records:detail', pk=record.pk)

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False, 'error': 'Please fill in all required fields.'}, status=400)
    return render(request, 'medical_records/upload_text.html', {'form': form})


# ─── Camera / OCR Scan ───────────────────────────────────────────────────────

@login_required
@scoped_ratelimit('ocr')
def scan_ocr(request):
    """AJAX: receive an image, return OCR text via Gemini Vision."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    image = request.FILES.get('image')
    if not image:
        return JsonResponse({'error': 'No image received'}, status=400)

    ok, msg = validate_upload(image, ALLOWED_EXTENSIONS['image'])
    if not ok:
        return JsonResponse({'error': msg}, status=400)

    result = MedicalRecordService.ocr_image(image.read(),
                                             mime_type=image.content_type or 'image/jpeg',
                                             user=request.user)
    if result.get('consent_required'):
        return JsonResponse(
            {'error': result['error'], 'consent_required': result['consent_required']},
            status=403,
        )
    if result.get('error'):
        return JsonResponse({'error': result['error']}, status=500)
    return JsonResponse({'text': result['text']})


@login_required
@scoped_ratelimit('upload')
def upload_scan(request):
    """Camera scan page: capture image → OCR → review text → save record."""
    is_ajax      = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    record_types = MedicalRecord.RecordType.choices

    if request.method == 'POST':
        raw_text    = request.POST.get('raw_text', '').strip()
        title       = request.POST.get('title', '').strip()
        record_type = request.POST.get('record_type', MedicalRecord.RecordType.OTHER)
        if record_type not in MedicalRecord.RecordType.values:
            record_type = MedicalRecord.RecordType.OTHER
        notes       = request.POST.get('notes', '').strip()
        date_str    = request.POST.get('record_date', '').strip()

        from django.conf import settings as _s
        if len(raw_text) > _s.MAX_PASTED_TEXT_CHARS:
            msg = f'Text is too long. The limit is {_s.MAX_PASTED_TEXT_CHARS:,} characters.'
            if is_ajax:
                return JsonResponse({'success': False, 'error': msg}, status=400)
            messages.error(request, msg)
            return render(request, 'medical_records/upload_scan.html', {'record_types': record_types})
        if not raw_text:
            if is_ajax:
                return JsonResponse({'success': False,
                                     'error': 'No text provided. Please extract text first.'}, status=400)
            messages.error(request, 'No text extracted. Please capture and extract text first.')
            return render(request, 'medical_records/upload_scan.html', {'record_types': record_types})

        date_override = None
        if date_str:
            try:
                date_override = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        result = MedicalRecordService.create_from_text(
            request.user, raw_text,
            notes=notes,
            record_type=record_type,
            title_override=title,
            date_override=date_override,
        )

        record  = result['record']
        summary = 'Record saved from scanned image.'
        if result['structured']:
            summary += ' AI extracted structured data.'
        if result['flagged']:
            summary += f' ⚠️ {result["flagged"]} abnormal value(s) detected.'

        if is_ajax:
            return JsonResponse({'success': True, 'record': _record_json(record),
                                 'message': summary, 'flagged': bool(result['flagged'])})
        if result['flagged']:
            messages.warning(request, summary)
        else:
            messages.success(request, summary)
        return redirect('medical_records:detail', pk=record.pk)

    if is_ajax and request.method == 'POST':
        return JsonResponse({'success': False,
                             'error': 'Please provide extracted text before saving.'}, status=400)
    return render(request, 'medical_records/upload_scan.html', {'record_types': record_types})


# ─── Helpers (presentation only) ─────────────────────────────────────────────

def _failed_inline(record) -> bool:
    """The job ran inline (no worker) and failed: answer like the old
    synchronous upload did, with an error and no half-made record."""
    if record.processing_status == MedicalRecord.ProcessingStatus.FAILED:
        record.delete()
        return True
    return False


def _upload_summary(record, done_message: str) -> str:
    if record.processing_status in (MedicalRecord.ProcessingStatus.QUEUED,
                                    MedicalRecord.ProcessingStatus.PROCESSING):
        return 'Received. The record is being processed and will update in a moment.'
    if record.parsed_data:
        return done_message + ' AI extracted structured data.'
    return done_message


def _record_json(record):
    return {
        'pk':                  str(record.pk),
        'processing_status':   record.processing_status,
        'status_url':          reverse('medical_records:status', kwargs={'pk': record.pk}),
        'title':               record.title,
        'record_type':         record.record_type,
        'record_type_display': record.get_record_type_display(),
        'source_display':      record.get_source_display(),
        'record_date':         date_format(record.record_date) if record.record_date else None,
        'uploaded_at':         date_format(record.uploaded_at),
        'is_flagged':          record.is_flagged,
        'detail_url':          reverse('medical_records:detail', kwargs={'pk': record.pk}),
        'delete_url':          reverse('medical_records:delete', kwargs={'pk': record.pk}),
    }


# ─── Processing status (polled by the records pages) ─────────────────────────

@login_required
def record_status(request, pk):
    record = get_object_or_404(MedicalRecord, pk=pk, patient=request.user)
    return JsonResponse({'status': record.processing_status,
                         'status_display': record.get_processing_status_display(),
                         'error': record.processing_error})


@login_required
@require_POST
@scoped_ratelimit('upload')
def record_retry(request, pk):
    record = get_object_or_404(MedicalRecord, pk=pk, patient=request.user)
    if retry_processing(record):
        messages.success(request, 'Processing again.')
    else:
        messages.info(request, 'This record does not need processing.')
    return redirect('medical_records:detail', pk=record.pk)


# ─── One "Add record" entry (audit UX-05) ─────────────────────────────────────

@login_required
@require_POST
@scoped_ratelimit('upload')
def upload_any(request):
    """Drop any file or paste text: the type is decided here from the validated
    extension and routed to the same service the typed uploads use."""
    from django.conf import settings as _s

    from .services import upload_kind_for

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def fail(message, code=400):
        if is_ajax:
            return JsonResponse({'success': False, 'error': message}, status=code)
        messages.error(request, message)
        return redirect('medical_records:list')

    upload = request.FILES.get('file')
    text = (request.POST.get('text') or '').strip()
    notes = request.POST.get('notes', '')
    if not upload and not text:
        return fail('Choose a file or paste some text.')
    if text and not upload:
        if len(text) > _s.MAX_PASTED_TEXT_CHARS:
            return fail(f'Text is too long. The limit is {_s.MAX_PASTED_TEXT_CHARS:,} characters.')
        result = queue_text(request.user, text, notes=notes)
        return _upload_done(request, is_ajax, result['record'], 'Record saved from pasted text.')

    kind = upload_kind_for(upload.name)
    if kind is None:
        allowed = ', '.join(e for exts in ALLOWED_EXTENSIONS.values() for e in exts)
        return fail(f'This file type is not supported. Accepted: {allowed}.')
    ok, safe_name = validate_upload(upload, ALLOWED_EXTENSIONS[kind])
    if not ok:
        return fail(safe_name)

    if kind == 'pdf':
        record = queue_pdf(request.user, upload.read(), notes=notes, filename=safe_name)['record']
        if _failed_inline(record):
            return fail('This PDF could not be read. Please check the file.')
        return _upload_done(request, is_ajax, record, 'Document uploaded and parsed.')
    if kind == 'kanta':
        result = MedicalRecordService.create_from_kanta(request.user, upload.read(), notes=notes)
        if result.get('error'):
            return fail(result['error'])
        message = f"Imported {result['records_created']} record(s) from Kanta."
        if is_ajax:
            return JsonResponse({'success': True, 'message': message, 'reload': True})
        messages.success(request, message)
        return redirect('medical_records:list')
    if kind == 'wearable':
        result = MedicalRecordService.create_from_wearable(request.user, upload.read(),
                                                           filename=safe_name, notes=notes)
        if result.get('error'):
            return fail(result['error'])
        return _upload_done(request, is_ajax, result['record'], 'Wearable data imported.')
    # An image: read the text with OCR, then treat it as pasted text.
    result = MedicalRecordService.ocr_image(upload.read(), mime_type=upload.content_type or 'image/jpeg',
                                            user=request.user)
    if result.get('error'):
        return fail(result['error'], 403 if result.get('consent_required') else 400)
    record = queue_text(request.user, result['text'], notes=notes)['record']
    return _upload_done(request, is_ajax, record, 'Record saved from the scanned image.')


def _upload_done(request, is_ajax, record, done_message):
    summary = _upload_summary(record, done_message)
    if is_ajax:
        return JsonResponse({'success': True, 'record': _record_json(record), 'message': summary,
                             'flagged': record.is_flagged})
    messages.success(request, summary)
    return redirect('medical_records:detail', pk=record.pk)


@login_required
def record_edit(request, pk):
    from .forms import RecordEditForm

    record = get_object_or_404(MedicalRecord, pk=pk, patient=request.user)
    form = RecordEditForm(request.POST or None, instance=record)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Record updated.')
        return redirect('medical_records:detail', pk=record.pk)
    return render(request, 'medical_records/edit.html', {'form': form, 'record': record})

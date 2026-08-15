import logging
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from .forms import KantaUploadForm, WearableUploadForm, PDFUploadForm, TextPasteForm
from .models import MedicalRecord
from .services import MedicalRecordService, validate_upload

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

    if period in PERIOD_CUTOFFS:
        cutoff = PERIOD_CUTOFFS[period]
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
    })


# ─── Record Detail ────────────────────────────────────────────────────────────

@login_required
def record_detail(request, pk):
    record = get_object_or_404(MedicalRecord, pk=pk, patient=request.user)
    return render(request, 'medical_records/detail.html', {
        'record':          record,
        'lab_values':      record.lab_values.all(),
        'wearable_points': record.wearable_points.all()[:200],
        # What THIS document asserted, not the resolved state. A discharge
        # summary listing a drug is not a claim that the patient is on it today,
        # and showing it next to the record as though it were would put a
        # superseded medication in front of someone as current.
        'medications':     record.medicationstatement_set.all(),
        'conditions':      record.conditionstatement_set.all(),
    })


# ─── Medications & Conditions ─────────────────────────────────────────────────

@login_required
def health_summary(request):
    """
    What the patient is on and what they have, resolved across every document.

    Their own data, so no predicate stands between them and it — the whole
    authorization question here is that `request.user` is the only patient this
    can be called for. Recipients and clinicians reach the same summary through
    their own gated views.
    """
    from .clinical_state import clinical_summary

    context = {'subject': request.user, 'is_own': True}
    context.update(clinical_summary(request.user))
    return render(request, 'medical_records/health_summary.html', context)


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

        ok, msg = validate_upload(xml_file, ['xml'])
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
def upload_wearable(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = WearableUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        data_file = request.FILES['data_file']

        ok, msg = validate_upload(data_file, ['csv', 'json', 'parquet', 'xlsx'])
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
def upload_pdf(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = PDFUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        pdf_file = request.FILES['pdf_file']

        ok, safe_name = validate_upload(pdf_file, ['pdf'])
        if not ok:
            if is_ajax:
                return JsonResponse({'success': False, 'error': safe_name}, status=400)
            messages.error(request, safe_name)
            return render(request, 'medical_records/upload_pdf.html', {'form': form})

        result = MedicalRecordService.create_from_pdf(
            request.user, pdf_file.read(),
            notes=form.cleaned_data.get('notes', ''),
            record_type=form.cleaned_data.get('record_type') or None,
            filename=safe_name,
        )
        if result.get('error'):
            err = f'PDF parse error: {result["error"]}'
            if is_ajax:
                return JsonResponse({'success': False, 'error': err}, status=400)
            messages.error(request, err)
            return render(request, 'medical_records/upload_pdf.html', {'form': form})

        record  = result['record']
        summary = f'Document uploaded and parsed ({result["page_count"]} page(s)).'
        if result['structured']:
            summary += ' AI extracted structured data.'

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
def upload_text(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = TextPasteForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        result = MedicalRecordService.create_from_text(
            request.user,
            form.cleaned_data['text'],
            notes=form.cleaned_data.get('notes', ''),
            record_type=form.cleaned_data['record_type'],
        )

        record  = result['record']
        summary = 'Record saved from pasted text.'
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

    ok, msg = validate_upload(image, ['jpg', 'jpeg', 'png', 'gif', 'webp'])
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
def upload_scan(request):
    """Camera scan page: capture image → OCR → review text → save record."""
    is_ajax      = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    record_types = MedicalRecord.RecordType.choices

    if request.method == 'POST':
        raw_text    = request.POST.get('raw_text', '').strip()
        title       = request.POST.get('title', '').strip()
        record_type = request.POST.get('record_type', MedicalRecord.RecordType.OTHER)
        notes       = request.POST.get('notes', '').strip()
        date_str    = request.POST.get('record_date', '').strip()

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

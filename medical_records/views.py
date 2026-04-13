import logging
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404

from .forms import KantaUploadForm, WearableUploadForm, PDFUploadForm
from .models import MedicalRecord, ParsedLabValue, WearableDataPoint
from .parsers import KantaXMLParser, WearableCSVParser, PDFParser

logger = logging.getLogger(__name__)


# ─── Record List ─────────────────────────────────────────────────────────────

@login_required
def record_list(request):
    records = MedicalRecord.objects.filter(patient=request.user).order_by('-uploaded_at')

    # Filter by type
    rtype = request.GET.get('type', '')
    if rtype:
        records = records.filter(record_type=rtype)

    flagged_count = MedicalRecord.objects.filter(patient=request.user, is_flagged=True).count()

    return render(request, 'medical_records/list.html', {
        'records': records,
        'record_types': MedicalRecord.RecordType.choices,
        'active_type': rtype,
        'flagged_count': flagged_count,
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
    form = KantaUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        xml_file = request.FILES['xml_file']
        xml_bytes = xml_file.read()

        parser = KantaXMLParser()
        parsed = parser.parse(xml_bytes)

        if 'error' in parsed:
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
            messages.warning(request, msg)
        else:
            messages.success(request, msg)

        return redirect('medical_records:list')

    return render(request, 'medical_records/upload_kanta.html', {'form': form})


# ─── Wearable CSV Upload ──────────────────────────────────────────────────────

@login_required
def upload_wearable(request):
    form = WearableUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        csv_file = request.FILES['csv_file']
        csv_bytes = csv_file.read()

        parser = WearableCSVParser()
        parsed = parser.parse(csv_bytes, filename=csv_file.name)

        if not parsed.get('data_points'):
            messages.error(request, 'No data points found. Check CSV format.')
            return render(request, 'medical_records/upload_wearable.html', {'form': form})

        device = parsed.get('device', 'unknown')
        dp_count = parsed['count']

        record = MedicalRecord.objects.create(
            patient=request.user,
            record_type=MedicalRecord.RecordType.WEARABLE,
            source=MedicalRecord.Source.WEARABLE_CSV,
            title=f'{device.replace("_", " ").title()} — {csv_file.name}',
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
        messages.success(request, msg)
        return redirect('medical_records:list')

    return render(request, 'medical_records/upload_wearable.html', {'form': form})


# ─── PDF Upload ───────────────────────────────────────────────────────────────

@login_required
def upload_pdf(request):
    form = PDFUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        pdf_file = request.FILES['pdf_file']
        pdf_bytes = pdf_file.read()

        parser = PDFParser()
        parsed = parser.parse(pdf_bytes, use_ai=True)

        if parsed.get('error'):
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
        messages.success(request, msg)
        return redirect('medical_records:detail', pk=record.pk)

    return render(request, 'medical_records/upload_pdf.html', {'form': form})


# ─── Helpers ──────────────────────────────────────────────────────────────────

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
        from ai_insights.models import HealthAlert
        from notifications.models import Notification

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

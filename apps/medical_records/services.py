"""
MedicalRecordService — single source of truth for creating medical records.

Both the web views (apps/medical_records/views.py) and the mobile API
(apps/api/views.py) delegate here.  All parsing, unit normalisation, alert
creation, and RAG indexing happen exactly once.

Return convention for all create_* methods:
  success → {'record': MedicalRecord, ...}
  failure → {'error': str}
"""
import logging
import os
import re
from datetime import datetime

from django.db import transaction

from .models import MedicalRecord, ParsedLabValue, WearableDataPoint
from .unit_normalizer import normalize as normalize_lab_unit

logger = logging.getLogger(__name__)


# ── File-upload validation ────────────────────────────────────────────────────

_MAGIC = {
    'pdf':     [(0, b'%PDF-')],
    'xml':     [(0, b'<?xml'), (3, b'<?xml')],   # bare + UTF-8 BOM prefix
    'jpg':     [(0, b'\xff\xd8\xff')],
    'jpeg':    [(0, b'\xff\xd8\xff')],
    'png':     [(0, b'\x89PNG\r\n')],
    'gif':     [(0, b'GIF8')],
    'webp':    [(0, b'RIFF')],
    'parquet': [(0, b'PAR1')],
}

_UNSAFE_NAME_RE = re.compile(r'[^\w\-.]')
_MAX_FILENAME_LEN = 200


def validate_upload(file_obj, allowed_exts: list) -> tuple:
    """
    Validate an uploaded file by magic bytes and extension.

    Returns (ok: bool, payload: str) where payload is the sanitized filename
    on success, or a human-readable error message on failure.
    """
    raw_name  = os.path.basename(file_obj.name or 'upload')
    safe_name = _UNSAFE_NAME_RE.sub('_', raw_name)[:_MAX_FILENAME_LEN] or 'upload'
    ext       = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''

    if allowed_exts and ext not in allowed_exts:
        return False, f'File type ".{ext}" not allowed. Accepted: {", ".join(allowed_exts)}'

    if ext in _MAGIC:
        header  = file_obj.read(16)
        file_obj.seek(0)
        matched = any(header[off:off + len(sig)] == sig for off, sig in _MAGIC[ext])
        if not matched:
            return False, f'File content does not match its declared type (.{ext}). Upload rejected.'

    return True, safe_name


# ── Shared helpers ────────────────────────────────────────────────────────────

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
    name = entry.get('name', '').lower()
    try:
        value = float(str(entry.get('value', '')).replace(',', '.'))
    except (ValueError, TypeError):
        return False
    thresholds = {
        'hemoglobin': (value < 70 or value > 200),
        'glucose':    (value < 2.5 or value > 25),
        'potassium':  (value < 2.5 or value > 6.5),
        'sodium':     (value < 120 or value > 160),
        'creatinine': value > 1000,
        'troponin':   value > 10,
    }
    return any(k in name and v for k, v in thresholds.items())


def _create_alert(record: MedicalRecord, abnormal_count: int) -> None:
    try:
        from apps.ai_insights.models import HealthAlert
        from apps.notifications.models import Notification

        with transaction.atomic():
            HealthAlert.objects.create(
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
    except Exception as exc:
        logger.warning('Could not create alert for record %s: %s', record.pk, exc)


def _save_lab_value(record, lv: dict, *, is_critical: bool = False,
                    measured_at=None) -> bool:
    """Create one ParsedLabValue row. Returns True if is_abnormal."""
    _val_str = str(lv.get('value', ''))
    _canonical, _canon_unit, _orig_unit, _unit_known = normalize_lab_unit(
        lv.get('name', ''), _val_str, lv.get('unit', ''),
    )
    is_ab = lv.get('is_abnormal', False) or is_critical
    ParsedLabValue.objects.create(
        record=record,
        parameter_name=lv.get('name', 'Unknown'),
        value=_val_str,
        unit=_canon_unit,
        canonical_value=_canonical,
        original_unit=_orig_unit,
        unit_known=_unit_known,
        reference_range=lv.get('ref_range', '') or lv.get('reference_range', ''),
        is_abnormal=is_ab,
        is_critical=is_critical,
        measured_at=measured_at,
    )
    return is_ab


# ── Service ───────────────────────────────────────────────────────────────────

class MedicalRecordService:

    @staticmethod
    def create_from_pdf(user, pdf_bytes: bytes, *,
                        notes: str = '',
                        record_type=None,
                        filename: str = 'document.pdf') -> dict:
        from django.core.files.base import ContentFile
        from .parsers import PDFParser

        parsed = PDFParser().parse(pdf_bytes, use_ai=True)
        if parsed.get('error'):
            return {'error': parsed['error']}

        structured = parsed.get('structured') or {}
        rtype  = record_type or structured.get('record_type') or MedicalRecord.RecordType.OTHER
        title  = structured.get('title') or filename.rsplit('.', 1)[0]

        rec_date = None
        if structured.get('date'):
            try:
                rec_date = datetime.strptime(structured['date'], '%Y-%m-%d').date()
            except ValueError:
                pass

        with transaction.atomic():
            record = MedicalRecord.objects.create(
                patient=user,
                record_type=rtype,
                source=MedicalRecord.Source.MANUAL_UPLOAD,
                title=title,
                file=ContentFile(pdf_bytes, name=filename),
                raw_text=parsed.get('raw_text', ''),
                parsed_data=structured,
                notes=notes,
                record_date=rec_date,
            )

            flagged = sum(
                1 for lv in structured.get('lab_values', [])
                if _save_lab_value(record, lv)
            )
            if flagged:
                record.is_flagged = True
                record.save(update_fields=['is_flagged'])
                _create_alert(record, flagged)

        return {
            'record':     record,
            'flagged':    flagged,
            'page_count': parsed.get('page_count', 0),
            'structured': bool(structured),
        }

    @staticmethod
    def create_from_text(user, raw_text: str, *,
                         notes: str = '',
                         record_type: str = 'auto',
                         title_override: str = '',
                         date_override=None) -> dict:
        from .parsers import TextParser

        parsed     = TextParser().parse(raw_text)
        structured = parsed.get('structured') or {}

        rtype = (structured.get('record_type', MedicalRecord.RecordType.OTHER)
                 if record_type == 'auto' else record_type)
        title = title_override or structured.get('title') or raw_text[:60].strip().replace('\n', ' ')

        rec_date = date_override
        if rec_date is None and structured.get('date'):
            try:
                rec_date = datetime.strptime(structured['date'], '%Y-%m-%d').date()
            except ValueError:
                pass

        with transaction.atomic():
            record = MedicalRecord.objects.create(
                patient=user,
                record_type=rtype,
                source=MedicalRecord.Source.MANUAL_UPLOAD,
                title=title,
                raw_text=raw_text,
                parsed_data=structured,
                notes=notes,
                record_date=rec_date,
            )

            flagged = sum(
                1 for lv in structured.get('lab_values', [])
                if _save_lab_value(record, lv)
            )
            if flagged:
                record.is_flagged = True
                record.save(update_fields=['is_flagged'])
                _create_alert(record, flagged)

        try:
            from apps.rag_assistant.services.rag_service import RAGService
            RAGService().index_record(record)
        except Exception as exc:
            logger.warning('RAG indexing failed for record %s: %s', record.pk, exc)

        return {
            'record':     record,
            'flagged':    flagged,
            'structured': bool(structured),
        }

    @staticmethod
    def create_from_kanta(user, xml_bytes: bytes, *, notes: str = '') -> dict:
        from .parsers import KantaXMLParser

        parsed = KantaXMLParser().parse(xml_bytes)
        if 'error' in parsed:
            return {'error': parsed['error']}

        records_created = 0
        lab_values_created = 0
        total_flagged = 0
        record_ids = []

        for doc in parsed.get('records', []):
            rtype    = _map_doc_type(doc.get('type', ''))
            rec_date = None
            if doc.get('date'):
                try:
                    rec_date = datetime.strptime(doc['date'], '%Y-%m-%d').date()
                except ValueError:
                    pass

            with transaction.atomic():
                record = MedicalRecord.objects.create(
                    patient=user,
                    record_type=rtype,
                    source=MedicalRecord.Source.KANTA_XML,
                    title=doc.get('title') or doc.get('type') or 'Kanta Record',
                    parsed_data=doc,
                    notes=notes,
                    record_date=rec_date,
                )
                records_created += 1
                record_ids.append(str(record.pk))

                flagged = 0
                for section in doc.get('sections', []):
                    for entry in section.get('entries', []):
                        if entry.get('kind') != 'lab':
                            continue

                        is_critical = _check_critical(entry)

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

                        lv = {
                            'name':        entry.get('name', 'Unknown'),
                            'value':       entry.get('value', ''),
                            'unit':        entry.get('unit', ''),
                            'ref_range':   ref_range,
                            'is_abnormal': entry.get('is_abnormal', False),
                        }
                        if _save_lab_value(record, lv,
                                           is_critical=is_critical,
                                           measured_at=measured_at):
                            flagged += 1
                        lab_values_created += 1

                if flagged:
                    record.is_flagged = True
                    record.save(update_fields=['is_flagged'])
                    _create_alert(record, flagged)
                    total_flagged += flagged

        return {
            'records_created':    records_created,
            'lab_values_created': lab_values_created,
            'flagged':            total_flagged,
            'record_ids':         record_ids,
        }

    @staticmethod
    def create_from_wearable(user, data_bytes: bytes, *,
                             filename: str = '',
                             notes: str = '') -> dict:
        from .parsers import WearableParser

        try:
            parsed = WearableParser().parse(data_bytes, filename=filename)
        except Exception as exc:
            return {'error': str(exc)}

        if not parsed.get('data_points'):
            errs = parsed.get('errors', [])
            return {
                'error': ('No data points found. ' + errs[0])
                         if errs else 'No data points found. Check file format.'
            }

        device   = parsed.get('device', 'unknown')
        dp_count = parsed['count']

        objs = []
        for dp in parsed['data_points']:
            try:
                recorded_at = datetime.fromisoformat(dp['recorded_at'])
            except (ValueError, KeyError):
                continue
            objs.append(WearableDataPoint(
                metric=dp.get('metric', 'other'),
                value=dp['value'],
                unit=dp.get('unit', ''),
                recorded_at=recorded_at,
            ))

        with transaction.atomic():
            record = MedicalRecord.objects.create(
                patient=user,
                record_type=MedicalRecord.RecordType.WEARABLE,
                source=MedicalRecord.Source.WEARABLE_CSV,
                title=f'{device.replace("_", " ").title()} — {filename}',
                parsed_data={'device': device, 'count': dp_count},
                notes=notes,
            )
            for obj in objs:
                obj.record = record
            WearableDataPoint.objects.bulk_create(objs, batch_size=500)

        return {
            'record':      record,
            'data_points': len(objs),
            'device':      device,
            'errors':      parsed.get('errors', []),
        }

    @staticmethod
    def ocr_image(image_bytes: bytes, mime_type: str = 'image/jpeg') -> dict:
        from django.conf import settings as s
        try:
            from google import genai
            from google.genai import types

            api_key = getattr(s, 'GEMINI_API_KEY', '')
            if not api_key:
                return {'error': 'Gemini API key not configured'}

            client   = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    (
                        'Extract all text from this medical document image. '
                        'Return only the raw text content exactly as it appears, '
                        'preserving structure (line breaks, sections, tables). '
                        'Do not add commentary or explanations.'
                    ),
                ],
            )
            return {'text': (response.text or '').strip()}
        except Exception as exc:
            logger.warning('OCR error: %s', exc)
            return {'error': str(exc)}

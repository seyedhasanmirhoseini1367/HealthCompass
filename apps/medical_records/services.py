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

#: Extension -> list of accepted signatures. A signature is a list of
#: (offset, bytes) pairs that must ALL match, so a format needing two anchors
#: can express that.
_MAGIC = {
    'pdf':     [[(0, b'%PDF-')]],
    'xml':     [[(0, b'<?xml')], [(3, b'<?xml')]],   # bare + UTF-8 BOM prefix
    'jpg':     [[(0, b'\xff\xd8\xff')]],
    'jpeg':    [[(0, b'\xff\xd8\xff')]],
    'png':     [[(0, b'\x89PNG\r\n')]],
    'gif':     [[(0, b'GIF8')]],
    # RIFF alone is not WebP — it is the container used by WAV and AVI too, so
    # a renamed .wav passed this check. WebP additionally carries 'WEBP' at
    # offset 8.
    'webp':    [[(0, b'RIFF'), (8, b'WEBP')]],
    'parquet': [[(0, b'PAR1')]],
    # xlsx is a ZIP container; anything not starting with the local-file-header
    # signature is not a workbook whatever it is named. Note this accepts any
    # ZIP — decompression bounds are a separate concern (see MAX_PARSED_ROWS,
    # which bounds rows but not expansion).
    'xlsx':    [[(0, b'PK\x03\x04')]],
}

#: Text formats have no signature to check. Rejecting NUL bytes is the one
#: cheap structural test available: a text document does not contain them, and
#: a binary payload renamed .csv almost always does.
_TEXT_EXTS = frozenset({'csv', 'json', 'txt', 'tsv'})

#: Extensions accepted as images anywhere in the app. SVG is deliberately absent:
#: it is an XML document that can carry <script>, and serving one from our own
#: origin would run that script as first-party code.
IMAGE_EXTS = ['jpg', 'jpeg', 'png', 'gif', 'webp']

_UNSAFE_NAME_RE = re.compile(r'[^\w\-.]')
_MAX_FILENAME_LEN = 200
#: Extensions are short. Bounding this stops a pathological name from consuming
#: the whole budget and leaving no stem.
_MAX_EXT_LEN = 16


#: What each upload kind accepts. Validation and the HTML `accept` attributes
#: are both derived from this, so they cannot disagree again (the wearable
#: picker offered .xml, which was rejected, and hid .xlsx, which was accepted).
ALLOWED_EXTENSIONS = {
    'pdf':      ['pdf'],
    'kanta':    ['xml'],
    'wearable': ['csv', 'json', 'parquet', 'xlsx'],
    'image':    ['jpg', 'jpeg', 'png', 'gif', 'webp'],
}


def accept_attr(kind: str) -> str:
    """The `accept` attribute value for a file input of this upload kind."""
    return ','.join(f'.{ext}' for ext in ALLOWED_EXTENSIONS[kind])


def upload_kind_for(filename: str):
    """The upload kind a file's extension belongs to, or None."""
    ext = os.path.splitext(filename or '')[1].lower().lstrip('.')
    for kind, exts in ALLOWED_EXTENSIONS.items():
        if ext in exts:
            return kind
    return None


def _max_upload_bytes() -> int:
    from django.conf import settings as s
    return int(getattr(s, 'MAX_UPLOAD_BYTES', 25 * 1024 * 1024))


def validate_upload(file_obj, allowed_exts: list) -> tuple:
    """
    Validate an uploaded file by size, extension and magic bytes.

    Returns (ok: bool, payload: str) where payload is the sanitized filename
    on success, or a human-readable error message on failure.

    The size check matters: Django's DATA_UPLOAD_MAX_MEMORY_SIZE is calculated
    against the request body *excluding* file upload data, and
    FILE_UPLOAD_MAX_MEMORY_SIZE is only the spool-to-disk threshold — neither
    caps how large an uploaded file may be. Without this, uploads are unbounded.
    """
    raw_name = os.path.basename((file_obj.name or 'upload').replace('\\', '/'))
    cleaned  = _UNSAFE_NAME_RE.sub('_', raw_name)

    # Truncate the STEM, not the whole name. Cutting at a fixed length removed
    # the extension from long filenames, so a legitimate 250-character Kanta
    # export was rejected as having no type at all.
    stem, dot, ext_part = cleaned.rpartition('.')
    if dot and ext_part:
        ext_part  = ext_part[:_MAX_EXT_LEN]
        stem      = stem[:_MAX_FILENAME_LEN - len(ext_part) - 1] or 'upload'
        safe_name = f'{stem}.{ext_part}'
        ext       = ext_part.lower()
    else:
        safe_name = cleaned[:_MAX_FILENAME_LEN] or 'upload'
        ext       = ''

    limit = _max_upload_bytes()
    size = getattr(file_obj, 'size', None)
    if size is None:
        # A file object without .size previously skipped the limit entirely.
        # Measure it rather than waving it through — an unbounded upload is the
        # thing this check exists to prevent.
        try:
            file_obj.seek(0, os.SEEK_END)
            size = file_obj.tell()
            file_obj.seek(0)
        except Exception:
            return False, 'Could not determine the size of this upload. Upload rejected.'
    if size > limit:
        return False, (f'File is too large ({size // (1024 * 1024)} MB). '
                       f'Maximum accepted size is {limit // (1024 * 1024)} MB.')

    if allowed_exts and ext not in allowed_exts:
        return False, f'File type ".{ext}" not allowed. Accepted: {", ".join(allowed_exts)}'

    if ext in _MAGIC:
        header = file_obj.read(32)
        file_obj.seek(0)
        # Every (offset, bytes) pair in a signature must match; any one
        # signature matching is enough.
        matched = any(
            all(header[off:off + len(sig)] == sig for off, sig in signature)
            for signature in _MAGIC[ext]
        )
        if not matched:
            return False, f'File content does not match its declared type (.{ext}). Upload rejected.'

    elif ext in _TEXT_EXTS:
        header = file_obj.read(4096)
        file_obj.seek(0)
        if b'\x00' in header:
            return False, (f'This .{ext} file contains binary data. '
                           f'Upload rejected.')

    return True, safe_name


def validate_image_upload(file_obj) -> tuple:
    """
    Validate a user-supplied image by its actual bytes.

    The declared Content-Type is attacker-controlled and must never be the basis
    of this decision: an SVG announced as image/png was previously stored
    verbatim and then served from our own origin.
    """
    return validate_upload(file_obj, IMAGE_EXTS)


# ── Shared helpers ────────────────────────────────────────────────────────────

def content_fingerprint(payload) -> str:
    """
    SHA-256 over the identifying content of an ingested artifact.

    `payload` may be bytes (a file), a string (pasted text), or a JSON-safe
    structure (one document out of a Kanta bundle). Structures are serialised
    with sorted keys so an equivalent document always produces the same digest.

    Returns '' when there is nothing to fingerprint, which leaves the record
    exempt from de-duplication rather than colliding every empty upload into one
    row.
    """
    import hashlib
    import json

    if payload is None:
        return ''
    if isinstance(payload, bytes):
        data = payload
    elif isinstance(payload, str):
        data = payload.encode('utf-8')
    else:
        data = json.dumps(payload, sort_keys=True, default=str).encode('utf-8')

    if not data.strip():
        return ''
    return hashlib.sha256(data).hexdigest()


def find_duplicate(user, content_hash: str):
    """
    The already-ingested record for this artifact, if there is one.

    Scope is (patient, content_hash): the same document uploaded by two
    different patients is two records, because it is two people's data.
    """
    if not content_hash:
        return None
    return MedicalRecord.objects.filter(
        patient=user, content_hash=content_hash).first()


def _coerce_record_type(value, default=None):
    """
    Constrain a record type to the declared choices.

    Django does not validate `choices` on save(), so a value taken from a
    request body — or invented by an LLM parsing a document — is stored verbatim
    unless it is checked here. Anything unrecognised falls back rather than
    raising: the record is still worth keeping.
    """
    default = default or MedicalRecord.RecordType.OTHER
    valid = {c for c, _label in MedicalRecord.RecordType.choices}
    return value if value in valid else default



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


# ── Critical-value detection ──────────────────────────────────────────────────
#
# This used to compare the RAW uploaded number against thresholds written in SI
# units, before normalisation had happened. Two ways that went wrong:
#
#   * A US-format record reporting glucose 140 mg/dL evaluated 140 > 25 and was
#     flagged CRITICAL, firing a HealthAlert and a push notification.
#   * The check ran on the Kanta path only, so whether a genuinely critical
#     value was noticed depended on which file format the patient uploaded.
#
# Detection now happens inside _save_lab_value(), after normalisation, so all
# three ingestion paths share one implementation by construction.
#
# The thresholds themselves are unchanged clinically. They are declared in the
# SI units they were originally written in and converted to canonical units by
# `normalize()` at import, so the conversion factor has exactly one source of
# truth (unit_normalizer) and cannot drift from the one used on real values.
#
#: analyte -> (source_low, source_high, source_unit). None = no bound.
_CRITICAL_SOURCE_THRESHOLDS = {
    'hemoglobin': (70.0, 200.0, 'g/L'),      # -> g/dL   (÷10)
    'glucose':    (2.5,  25.0,  'mmol/L'),   # -> mg/dL  (×18.016)
    'creatinine': (None, 1000.0, 'µmol/L'),  # -> mg/dL  (÷88.4)
    # No conversion exists for these two, so `normalize()` passes them through
    # and the numbers stand as written.
    'potassium':  (2.5,  6.5,   'mmol/L'),
    'sodium':     (120.0, 160.0, 'mmol/L'),
}

# Units that are numerically interchangeable for these analytes: mEq/L equals
# mmol/L for singly-charged ions (K+, Na+). Accepting both preserves the
# detection that already worked for US-format records.
_UNIT_EQUIVALENTS = {'mmol/l': {'mmol/l', 'meq/l'}}

# Troponin previously carried a `> 10` rule. It is deliberately NOT included:
# the codebase has no conversion entry for it and no other reference, so the
# intended unit is undeterminable — ng/mL and ng/L differ by 1000×. Comparing
# without knowing which would be the exact defect this change exists to remove.
# Restore it by adding a threshold WITH its unit once that is established.


def _build_critical_thresholds() -> dict:
    """Convert the SI thresholds above into canonical units via normalize()."""
    from .unit_normalizer import normalize as _norm
    table = {}
    for analyte, (low, high, unit) in _CRITICAL_SOURCE_THRESHOLDS.items():
        canon_low, canon_unit, _o, _k = (
            _norm(analyte, str(low), unit) if low is not None else (None, None, None, True))
        canon_high, canon_unit_h, _o, _k = (
            _norm(analyte, str(high), unit) if high is not None else (None, None, None, True))
        table[analyte] = (canon_low, canon_high, (canon_unit or canon_unit_h or unit).lower())
    return table


_CRITICAL_THRESHOLDS = _build_critical_thresholds()


def _is_critical(parameter_name: str, canonical_value, canonical_unit: str,
                 unit_known: bool) -> bool:
    """
    Decide criticality from the CANONICAL representation only.

    Returns False — never a guess — when the value is unparseable, the unit was
    not recognised, or the unit does not match the one the threshold is written
    in. A missed alert is recoverable; an alert derived from an incompatible
    unit comparison is not.
    """
    if canonical_value is None or not unit_known:
        return False

    name = (parameter_name or '').lower()
    observed = (canonical_unit or '').lower().strip()

    # Longest name first: 'hemoglobin a1c' must not be matched by 'hemoglobin'.
    for analyte in sorted(_CRITICAL_THRESHOLDS, key=len, reverse=True):
        if analyte not in name:
            continue
        low, high, expected = _CRITICAL_THRESHOLDS[analyte]
        accepted = _UNIT_EQUIVALENTS.get(expected, {expected})
        if observed not in accepted:
            # Right analyte, wrong/unknown unit — refuse to compare.
            logger.warning(
                'Skipping critical check for %s: value is in %r but the '
                'threshold is defined in %r', parameter_name, canonical_unit, expected)
            return False
        if low is not None and canonical_value < low:
            return True
        if high is not None and canonical_value > high:
            return True
        return False
    return False


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
        # The patient is NOT notified of abnormal values when this happens.
        from healthcompass.observability import Event as OpsEvent, emit as ops_emit
        ops_emit(OpsEvent.ALERT_CREATION_FAILED, record_id=str(record.pk),
                 abnormal_count=abnormal_count, error_type=type(exc).__name__)



# Characters removed from names that come out of uploaded files: angle brackets
# (markup) and ASCII control characters. The name is shown on pages other people
# see (population statistics), so it is treated as hostile (audit SEC-01).
_UNSAFE_NAME_CHARS = re.compile(r'[<>\x00-\x1f\x7f]')
PARAMETER_NAME_MAX = 120


def _clean_label(value, max_len: int) -> str:
    text = _UNSAFE_NAME_CHARS.sub(' ', str(value or ''))
    return ' '.join(text.split())[:max_len].strip()


def normalise_parameter_name(name) -> str:
    """Strip markup and control characters, collapse whitespace, cap length.

    Returns 'Unknown' when nothing printable is left.
    """
    return _clean_label(name, PARAMETER_NAME_MAX) or 'Unknown'


def _save_lab_value(record, lv: dict, *, measured_at=None) -> bool:
    """
    Create one ParsedLabValue row. Returns True if is_abnormal.

    Criticality is decided HERE, immediately after normalisation, because this
    is the single point every ingestion path passes through. Callers must not
    pass a precomputed flag: doing so is what allowed the Kanta path to use a
    raw-value comparison while the PDF and text paths computed nothing at all.
    """
    name = normalise_parameter_name(lv.get('name'))
    _val_str = _clean_label(lv.get('value', ''), 100)
    _canonical, _canon_unit, _orig_unit, _unit_known = normalize_lab_unit(
        name, _val_str, _clean_label(lv.get('unit', ''), 50),
    )
    is_critical = _is_critical(name, _canonical, _canon_unit, _unit_known)
    is_ab = lv.get('is_abnormal', False) or is_critical
    ParsedLabValue.objects.create(
        record=record,
        parameter_name=name,
        value=_val_str,
        unit=_canon_unit,
        canonical_value=_canonical,
        original_unit=_orig_unit,
        unit_known=_unit_known,
        reference_range=_clean_label(lv.get('ref_range', '') or lv.get('reference_range', ''), 100),
        is_abnormal=is_ab,
        is_critical=is_critical,
        measured_at=measured_at,
    )
    return is_ab


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _parse_pdf(user, pdf_bytes, *, record_type=None, filename='document.pdf') -> dict:
    """Parse a PDF into record fields. Local extraction always runs; LLM
    structuring only with the owner's consent (records.parse)."""
    from apps.accounts.egress import ExternalProcessingGuard
    from .parsers import PDFParser

    use_ai = ExternalProcessingGuard.allows(user, 'records.parse')
    parsed = PDFParser().parse(pdf_bytes, use_ai=use_ai)
    if parsed.get('error'):
        return {'error': parsed['error']}
    structured = parsed.get('structured') or {}
    return {
        'structured':  structured,
        'raw_text':    parsed.get('raw_text', ''),
        'page_count':  parsed.get('page_count', 0),
        'record_type': _coerce_record_type(record_type or structured.get('record_type')),
        'title':       structured.get('title') or filename.rsplit('.', 1)[0],
        'record_date': _parse_date(structured.get('date')),
    }


def _parse_text(user, raw_text, *, record_type='auto', title_override='', date_override=None) -> dict:
    from apps.accounts.egress import ExternalProcessingGuard
    from .parsers import TextParser

    use_ai = ExternalProcessingGuard.allows(user, 'records.parse')
    structured = TextParser().parse(raw_text, use_ai=use_ai).get('structured') or {}
    return {
        'structured':  structured,
        'record_type': _coerce_record_type(
            structured.get('record_type') if record_type == 'auto' else record_type),
        'title':       title_override or structured.get('title')
                       or raw_text[:60].strip().replace('\n', ' '),
        'record_date': date_override or _parse_date(structured.get('date')),
    }


# ── Queued ingestion (audit ARCH-02) ─────────────────────────────────────────
#
# The upload request stores the input and returns at once; parsing, LLM
# structuring and lab extraction run in a job on the 'ingest' queue. Under the
# test runner, and without a worker, the job runs inline, so callers see the
# same result either way.

PROCESSING_FAILED_MESSAGE = 'This file could not be processed. You can try again.'


def queue_pdf(user, pdf_bytes: bytes, *, notes='', record_type=None,
              filename='document.pdf') -> dict:
    from django.core.files.base import ContentFile

    digest = content_fingerprint(pdf_bytes)
    existing = find_duplicate(user, digest)
    if existing is not None:
        return {'record': existing, 'flagged': 0, 'duplicate': True}
    record = MedicalRecord.objects.create(
        patient=user, record_type=_coerce_record_type(record_type),
        source=MedicalRecord.Source.MANUAL_UPLOAD, title=filename.rsplit('.', 1)[0],
        file=ContentFile(pdf_bytes, name=filename), content_hash=digest, notes=notes,
        processing_status=MedicalRecord.ProcessingStatus.QUEUED,
        parsed_data={'_ingest': {'kind': 'pdf', 'record_type': record_type or '',
                                 'filename': filename}},
    )
    _enqueue_processing(record)
    record.refresh_from_db()
    return {'record': record, 'flagged': _flag_count(record), 'queued': True}


def queue_text(user, raw_text: str, *, notes='', record_type='auto', title_override='',
               date_override=None) -> dict:
    digest = content_fingerprint(raw_text)
    existing = find_duplicate(user, digest)
    if existing is not None:
        return {'record': existing, 'flagged': 0, 'duplicate': True}
    record = MedicalRecord.objects.create(
        patient=user, record_type=MedicalRecord.RecordType.OTHER,
        source=MedicalRecord.Source.MANUAL_UPLOAD,
        title=title_override or raw_text[:60].strip().replace('\n', ' '),
        content_hash=digest, raw_text=raw_text, notes=notes, record_date=date_override,
        processing_status=MedicalRecord.ProcessingStatus.QUEUED,
        parsed_data={'_ingest': {'kind': 'text', 'record_type': record_type or 'auto',
                                 'title_override': title_override}},
    )
    _enqueue_processing(record)
    record.refresh_from_db()
    return {'record': record, 'flagged': _flag_count(record), 'queued': True}


def _flag_count(record) -> int:
    return record.lab_values.filter(is_abnormal=True).count() if record.is_flagged else 0


def _enqueue_processing(record):
    from healthcompass.tasks import enqueue
    enqueue(process_record, str(record.pk), queue='ingest')


def retry_processing(record) -> bool:
    """Re-run a failed job. Returns False when the record is not retryable."""
    if record.processing_status != MedicalRecord.ProcessingStatus.FAILED:
        return False
    MedicalRecord.objects.filter(pk=record.pk).update(
        processing_status=MedicalRecord.ProcessingStatus.QUEUED, processing_error='')
    _enqueue_processing(record)
    return True


def process_record(record_pk: str) -> None:
    """The job: parse the stored input and fill in the record.

    Claimed with a compare-and-set from QUEUED, so two workers never process one
    record. Any failure marks it FAILED with a generic message (never exception
    text, which can quote the document) and emits an ops event.
    """
    from healthcompass.observability import Event as OpsEvent, emit as ops_emit

    S = MedicalRecord.ProcessingStatus
    claimed = MedicalRecord.objects.filter(pk=record_pk, processing_status=S.QUEUED).update(
        processing_status=S.PROCESSING)
    if not claimed:
        return
    record = MedicalRecord.objects.get(pk=record_pk)
    spec = (record.parsed_data or {}).get('_ingest', {})
    try:
        if spec.get('kind') == 'pdf':
            with record.file.open('rb') as fh:
                pdf_bytes = fh.read()
            parsed = _parse_pdf(record.patient, pdf_bytes, record_type=spec.get('record_type') or None,
                                filename=spec.get('filename') or 'document.pdf')
            if parsed.get('error'):
                raise ValueError('parse error')
            record.raw_text = parsed['raw_text']
        else:
            parsed = _parse_text(record.patient, record.raw_text,
                                 record_type=spec.get('record_type') or 'auto',
                                 title_override=spec.get('title_override') or '',
                                 date_override=record.record_date)
        with transaction.atomic():
            record.lab_values.all().delete()
            record.record_type = parsed['record_type']
            record.title = parsed['title']
            record.record_date = parsed['record_date'] or record.record_date
            record.parsed_data = parsed['structured']
            flagged = sum(1 for lv in parsed['structured'].get('lab_values', [])
                          if _save_lab_value(record, lv))
            record.is_flagged = bool(flagged)
            record.processing_status = S.READY
            record.processing_error = ''
            # Content fields in update_fields: this save triggers indexing.
            record.save(update_fields=['raw_text', 'record_type', 'title', 'record_date',
                                       'parsed_data', 'is_flagged', 'processing_status',
                                       'processing_error'])
            if flagged:
                _create_alert(record, flagged)
    except Exception as exc:
        MedicalRecord.objects.filter(pk=record_pk).update(
            processing_status=S.FAILED, processing_error=PROCESSING_FAILED_MESSAGE)
        logger.warning('Processing record %s failed: %s', record_pk, type(exc).__name__)
        ops_emit(OpsEvent.INGESTION_PARSE_FAILED, record_id=str(record_pk),
                 error_type=type(exc).__name__)


# ── Service ───────────────────────────────────────────────────────────────────

class MedicalRecordService:

    @staticmethod
    def create_from_pdf(user, pdf_bytes: bytes, *,
                        notes: str = '',
                        record_type=None,
                        filename: str = 'document.pdf') -> dict:
        from django.core.files.base import ContentFile

        # Idempotency check first, before any parsing. Re-uploading a document
        # previously produced a second record and a second full set of lab
        # values; returning early also avoids re-running the Gemini extraction
        # and re-spending that quota on bytes already ingested.
        digest = content_fingerprint(pdf_bytes)
        existing = find_duplicate(user, digest)
        if existing is not None:
            logger.info('create_from_pdf: identical document already ingested '
                        '(record %s) — returning it unchanged', existing.pk)
            return {'record': existing, 'flagged': 0, 'page_count': 0,
                    'structured': bool(existing.parsed_data), 'duplicate': True}

        parsed = _parse_pdf(user, pdf_bytes, record_type=record_type, filename=filename)
        if parsed.get('error'):
            return {'error': parsed['error']}
        structured, rtype, title, rec_date = (parsed['structured'], parsed['record_type'],
                                              parsed['title'], parsed['record_date'])

        with transaction.atomic():
            record = MedicalRecord.objects.create(
                patient=user,
                record_type=rtype,
                source=MedicalRecord.Source.MANUAL_UPLOAD,
                title=title,
                file=ContentFile(pdf_bytes, name=filename),
                content_hash=digest,
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
        digest = content_fingerprint(raw_text)
        existing = find_duplicate(user, digest)
        if existing is not None:
            logger.info('create_from_text: identical content already ingested '
                        '(record %s) — returning it unchanged', existing.pk)
            return {'record': existing, 'flagged': 0,
                    'structured': bool(existing.parsed_data), 'duplicate': True}

        parsed = _parse_text(user, raw_text, record_type=record_type,
                             title_override=title_override, date_override=date_override)
        structured, rtype, title, rec_date = (parsed['structured'], parsed['record_type'],
                                              parsed['title'], parsed['record_date'])

        with transaction.atomic():
            record = MedicalRecord.objects.create(
                patient=user,
                record_type=rtype,
                source=MedicalRecord.Source.MANUAL_UPLOAD,
                title=title,
                content_hash=digest,
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

        # Indexing is NOT triggered here.
        #
        # This path used to call RAGService().index_record(record) explicitly
        # while the post_save signal was already indexing the same record — the
        # PDF and Kanta paths did not, so text uploads indexed twice and the
        # others once. Because process_record() deletes every document for a
        # record before recreating it, the two runs interleaved as
        # delete/delete/create/create and left two documents and two chunk sets
        # for one upload: duplicated evidence at retrieval time and double the
        # embedding cost. Ten records in the development database carry that
        # damage.
        #
        # The signal is now the single trigger for every ingestion path.

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
        records_skipped = 0          # already ingested from an earlier import
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

            # Fingerprint the individual document, not the upload. A Kanta
            # bundle produces one record per document, so hashing the XML would
            # make every document in it collide with its siblings. Re-importing
            # an export that overlaps a previous one is the common case here.
            doc_digest = content_fingerprint(doc)
            already = find_duplicate(user, doc_digest)
            if already is not None:
                records_skipped += 1
                continue

            with transaction.atomic():
                record = MedicalRecord.objects.create(
                    patient=user,
                    record_type=rtype,
                    source=MedicalRecord.Source.KANTA_XML,
                    title=doc.get('title') or doc.get('type') or 'Kanta Record',
                    content_hash=doc_digest,
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
                        if _save_lab_value(record, lv, measured_at=measured_at):
                            flagged += 1
                        lab_values_created += 1

                if flagged:
                    record.is_flagged = True
                    record.save(update_fields=['is_flagged'])
                    _create_alert(record, flagged)
                    total_flagged += flagged

        return {
            'records_created':    records_created,
            'records_skipped':    records_skipped,
            'lab_values_created': lab_values_created,
            'flagged':            total_flagged,
            'record_ids':         record_ids,
        }

    @staticmethod
    def create_from_wearable(user, data_bytes: bytes, *,
                             filename: str = '',
                             notes: str = '') -> dict:
        from .parsers import WearableParser

        # Re-uploading the same export previously duplicated every data point.
        digest = content_fingerprint(data_bytes)
        existing = find_duplicate(user, digest)
        if existing is not None:
            logger.info('create_from_wearable: identical export already ingested '
                        '(record %s) — returning it unchanged', existing.pk)
            return {'record': existing, 'data_points': 0, 'device': '',
                    'errors': [], 'duplicate': True}

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
                content_hash=digest,
                parsed_data={'device': device, 'count': dp_count},
                notes=notes,
            )
            # bulk_create does not call save(), so the denormalised owner is set
            # here explicitly. A row without it would be invisible to any
            # patient-scoped query.
            for obj in objs:
                obj.record = record
                obj.patient = user
            WearableDataPoint.objects.bulk_create(objs, batch_size=500)

        return {
            'record':      record,
            'data_points': len(objs),
            'device':      device,
            'errors':      parsed.get('errors', []),
        }

    @staticmethod
    def ocr_image(image_bytes: bytes, *, mime_type: str = 'image/jpeg', user) -> dict:
        """
        OCR an uploaded document image via Gemini vision.

        `user` — the owner of the image — is a REQUIRED keyword argument. It was
        briefly optional for caller compatibility, which meant a caller that
        forgot it got permissive behaviour by default: exactly the wrong failure
        mode for a function that ships a medical document to a third party.
        Omitting it now raises TypeError, so the image cannot be transmitted
        without someone having decided whose it is.

        Fail-closed twice over:
          1. An anonymous or missing user is refused outright, regardless of
             CONSENT_ENFORCED_EGRESS — OCR of a medical document by nobody is
             never legitimate, so this does not wait on the rollout switch.
          2. An authenticated user is then checked against the egress guard.

        Both checks run BEFORE the bytes reach any client. There is no local OCR
        fallback, so a refusal ends the operation rather than degrading it.
        """
        from django.conf import settings as s
        from apps.accounts.consent import ConsentRequired
        from apps.accounts.egress import ExternalProcessingGuard

        from apps.accounts.models import ConsentPurpose

        if user is None or not getattr(user, 'is_authenticated', False):
            return {
                'error': 'Document OCR requires a signed-in account.',
                'consent_required': ConsentPurpose.EXTERNAL_LLM,
            }

        try:
            ExternalProcessingGuard.check(user, 'records.ocr')
        except ConsentRequired as exc:
            return {'error': exc.message, 'consent_required': exc.purpose}

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

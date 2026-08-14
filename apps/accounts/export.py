"""
GDPR data export — Art. 15 (access) and Art. 20 (portability).

Produces a single ZIP containing one JSON file per data category plus the user's
original uploaded files. Everything is derived from the authenticated user
object passed in; no caller-supplied identifier is ever used to select the
subject, so there is no parameter that could be tampered with to export someone
else's data.

Design notes
------------
Synchronous by choice. The archive is written to a SpooledTemporaryFile that
rolls to disk past a few MB, so memory stays bounded and no job queue, artifact
store, signed URL or expiry mechanism is needed — none of which this project has
today. If exports ever outgrow a request, the seam to make asynchronous is
`build_export()`: it already returns a file object rather than a response.

Scope decisions are recorded in EXCLUSIONS below and mirrored into the manifest,
so the person reading the archive sees what was left out and why.

Logging: this module never logs archive contents, filenames or field values —
only the fact that an export ran, keyed by user id.
"""
import io
import json
import logging
import zipfile
from datetime import datetime, date
from decimal import Decimal
from tempfile import SpooledTemporaryFile
from uuid import UUID

from django.utils import timezone

logger = logging.getLogger(__name__)

EXPORT_VERSION = '1.0'

#: Roll the archive to disk past this size instead of holding it in memory.
_SPOOL_MAX_BYTES = 8 * 1024 * 1024

#: Hard ceiling per attached file. A pathological upload should not be able to
#: turn one export request into an unbounded response.
_MAX_FILE_BYTES = 60 * 1024 * 1024

#: Documented in the manifest so the archive explains its own omissions.
EXCLUSIONS = [
    {'item': 'password hash',
     'reason': 'Credential. Never disclosed, even to its owner.'},
    {'item': 'session and JWT tokens, Google OAuth tokens',
     'reason': 'Authentication credentials, not personal data.'},
    {'item': 'emergency card token',
     'reason': 'A capability secret: anyone holding it can read the public '
               'emergency card. Visible in the app under Emergency Card, but '
               'excluded from the archive so a leaked export cannot be replayed.'},
    {'item': 'push notification device tokens',
     'reason': 'Device credentials. Device count and timestamps are included instead.'},
    {'item': 'embedding vectors',
     'reason': 'Derived numeric representations of text already included in full. '
               'Their provenance (model, dimensions, date) is included.'},
    {'item': 'IP address hashes of emergency-card viewers',
     'reason': 'Pseudonymous identifiers of third parties, not the subject.'},
    {'item': "records of this user's access to OTHER patients' data",
     'reason': 'Applies to clinician/admin accounts. Those entries are the other '
               "patients' personal data, not this user's."},
    {'item': 'API keys, provider credentials, application settings',
     'reason': 'System configuration, not personal data.'},
    {'item': 'Django groups, permissions and admin action log',
     'reason': 'Internal authorization state and system administration records.'},
]


# ── JSON safety ───────────────────────────────────────────────────────────────

def _json_default(value):
    """Last-resort encoder for values a JSONField may legitimately hold."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f'<{len(bytes(value))} bytes omitted>'
    if isinstance(value, set):
        return sorted(str(v) for v in value)
    return str(value)


def _safe_json(payload) -> str:
    """
    Serialise defensively.

    `parsed_data`, `result` and `metadata` are JSONFields holding whatever a
    parser or model provider produced. They can contain NaN/Infinity (valid in
    Python, invalid in JSON), byte strings, or deeply nested junk. An export must
    not fail because one historical record is odd, so anything unserialisable is
    replaced with a readable placeholder rather than raising.
    """
    try:
        return json.dumps(payload, indent=2, default=_json_default, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        logger.warning('export: falling back to lenient JSON encoding (%s)', type(exc).__name__)
        return json.dumps(_scrub(payload), indent=2, default=_json_default, allow_nan=False)


def _scrub(value, depth=0):
    """Recursively replace values that strict JSON cannot represent."""
    if depth > 30:
        return '<nesting too deep>'
    if isinstance(value, float):
        # NaN / inf are the common offenders from numeric parsers.
        return value if value == value and value not in (float('inf'), float('-inf')) else None
    if isinstance(value, dict):
        return {str(k): _scrub(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, (str, int, bool, type(None))):
        return value
    return _json_default(value)


def _dt(value):
    return value.isoformat() if value else None


# ── Category builders ─────────────────────────────────────────────────────────
#
# Each returns a JSON-serialisable structure for one file in the archive. They
# take the already-authenticated user and query only that user's rows.

def _account(user):
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount

    profile = getattr(user, 'patient_profile', None)
    data = {
        'username':      user.username,
        'email':         user.email,
        'first_name':    user.first_name,
        'last_name':     user.last_name,
        'phone_number':  user.phone_number,
        'date_of_birth': _dt(user.date_of_birth),
        'role':          user.role,
        'is_active':     user.is_active,
        'date_joined':   _dt(user.date_joined),
        'last_login':    _dt(user.last_login),
        'has_profile_picture': bool(user.profile_picture),
        'email_addresses': [
            {'email': e.email, 'verified': e.verified, 'primary': e.primary}
            for e in EmailAddress.objects.filter(user=user)
        ],
        'connected_accounts': [
            # Provider identity only. Access/refresh tokens live on SocialToken
            # and are never read here.
            {'provider': s.provider, 'uid': s.uid, 'connected_at': _dt(s.date_joined)}
            for s in SocialAccount.objects.filter(user=user)
        ],
    }

    if profile is not None:
        data['patient_profile'] = {
            'blood_type':              profile.blood_type,
            'allergies':               profile.allergies,
            'emergency_contact_name':  profile.emergency_contact_name,
            'emergency_contact_phone': profile.emergency_contact_phone,
            # The subject's own national identifier. Included because it is
            # their personal data and Art. 15 covers it; flagged in the manifest
            # because it makes the archive highly sensitive.
            'national_id':             profile.national_id or '',
            'emergency_card_enabled':  profile.emergency_card_enabled,
        }

    for attr, key in (('doctor_profile', 'doctor_profile'),
                      ('scientist_profile', 'data_scientist_profile'),
                      ('hospital_admin_profile', 'hospital_admin_profile')):
        prof = getattr(user, attr, None)
        if prof is None:
            continue
        fields = {
            f.name: getattr(prof, f.name)
            for f in prof._meta.fields
            # approved_by identifies a different user; id/user are internal keys.
            if f.name not in ('id', 'user', 'approved_by')
        }
        data[key] = {k: (_dt(v) if hasattr(v, 'isoformat') else v) for k, v in fields.items()}

    return data


def _medical_records(user):
    from apps.medical_records.models import MedicalRecord

    out = []
    qs = (MedicalRecord.objects.filter(patient=user)
          .prefetch_related('lab_values', 'wearable_points')
          .order_by('-record_date', '-uploaded_at'))
    for rec in qs:
        out.append({
            'id':            str(rec.id),
            'record_type':   rec.record_type,
            'source':        rec.source,
            'title':         rec.title,
            'notes':         rec.notes,
            'record_date':   _dt(rec.record_date),
            'uploaded_at':   _dt(rec.uploaded_at),
            'updated_at':    _dt(rec.updated_at),
            'is_flagged':    rec.is_flagged,
            'raw_text':      rec.raw_text,
            'parsed_data':   rec.parsed_data,
            'attached_file': _archive_path_for(rec) if rec.file else None,
            'lab_values': [{
                'parameter_name':  lv.parameter_name,
                'value':           lv.value,
                'unit':            lv.unit,
                'canonical_value': lv.canonical_value,
                'original_unit':   lv.original_unit,
                'unit_known':      lv.unit_known,
                'reference_range': lv.reference_range,
                'is_abnormal':     lv.is_abnormal,
                'is_critical':     lv.is_critical,
                'measured_at':     _dt(lv.measured_at),
                # Both, deliberately. The export is everything held about the
                # subject, and a corrected value without the original hides what
                # the source document said and what any alert fired on.
                'corrections': [{
                    'value':           c.value,
                    'unit':            c.unit,
                    'canonical_value': c.canonical_value,
                    'reason':          c.reason,
                    'source':          c.source,
                    'corrected_by':    c.actor_label,
                    'corrected_at':    _dt(c.created_at),
                } for c in lv.corrections.all()],
                'effective_value': lv.effective().value,
            } for lv in rec.lab_values.all()],
            'wearable_points': [{
                'metric':      wp.metric,
                'value':       wp.value,
                'unit':        wp.unit,
                'recorded_at': _dt(wp.recorded_at),
            } for wp in rec.wearable_points.all()],
        })
    return out


def _conversations(user):
    from apps.rag_assistant.models import ChatSession

    out = []
    for session in (ChatSession.objects.filter(patient=user)
                    .prefetch_related('messages').order_by('created_at')):
        out.append({
            'id':         str(session.pk),
            'title':      session.title,
            'created_at': _dt(session.created_at),
            'updated_at': _dt(session.updated_at),
            'messages': [{
                'id':           str(m.pk),
                'asked_at':     _dt(m.created_at),
                'question':     m.query,
                'answer':       m.response,
                'sources':      m.sources,
                'llm_provider': m.llm_provider,
                'query_mode':   m.query_mode,
                'safety_routed': m.safety_routed,
                'triggered_rules': m.triggered_rules,
            } for m in session.messages.order_by('created_at')],
        })
    return out


def _rag_index(user):
    """
    Derived search index over the user's own records.

    Chunk text is included because it is the user's data; the embedding vectors
    are not, because they are a numeric restatement of that same text and would
    add megabytes of opaque floats. Their provenance is included so the user can
    see which third-party model processed their records.
    """
    from apps.rag_assistant.models import MedicalChunk, MedicalDocument

    documents = []
    for doc in MedicalDocument.objects.filter(patient=user).order_by('created_at'):
        documents.append({
            'id':            str(doc.pk),
            'title':         doc.title,
            'document_type': doc.document_type,
            'source_record': str(doc.record_id) if doc.record_id else None,
            'created_at':    _dt(doc.created_at),
            'metadata':      doc.metadata,
        })

    chunks = []
    for chunk in MedicalChunk.objects.filter(patient=user).order_by('document_id', 'chunk_index'):
        chunks.append({
            'id':          str(chunk.pk),
            'document':    str(chunk.document_id),
            'chunk_index': chunk.chunk_index,
            'content':     chunk.content,
            'metadata':    chunk.metadata,
            'embedding':   {
                'model':      chunk.embedding_model or None,
                'dimensions': chunk.embedding_dimensions,
                'created_at': _dt(chunk.embedded_at),
                'vector':     'excluded — see manifest',
            } if chunk.embedding is not None else None,
        })

    return {'documents': documents, 'chunks': chunks}


def _appointments(user):
    from apps.appointments.models import Appointment

    return [{
        'id':                   str(a.pk),
        'title':                a.title,
        'doctor_name':          a.doctor_name,
        'location':             a.location,
        'appointment_datetime': _dt(a.appointment_datetime),
        'notes':                a.notes,
        'is_completed':         a.is_completed,
        'is_cancelled':         a.is_cancelled,
        'reminders_requested':  {'24h': a.remind_24h, '3h': a.remind_3h,
                                 '2h': a.remind_2h, '1h': a.remind_1h},
        'created_at':           _dt(a.created_at),
    } for a in Appointment.objects.filter(patient=user).order_by('appointment_datetime')]


def _insights(user):
    from apps.ai_insights.models import AIModel, HealthAlert

    data = {
        'health_alerts': [{
            'id':            str(al.pk),
            'severity':      al.severity,
            'title':         al.title,
            'message':       al.message,
            'source_record': str(al.source_record_id) if al.source_record_id else None,
            'is_read':       al.is_read,
            'created_at':    _dt(al.created_at),
        } for al in HealthAlert.objects.filter(patient=user).order_by('-created_at')],
    }

    # Only present for data-scientist accounts: models this user submitted.
    submitted = AIModel.objects.filter(data_scientist=user).order_by('-created_at')
    if submitted.exists():
        data['submitted_ai_models'] = [{
            'id':          str(m.pk),
            'name':        m.name,
            'slug':        m.slug,
            'description': m.description,
            'category':    m.category,
            'status':      m.status,
            'created_at':  _dt(m.created_at),
            'run_count':   m.run_count,
        } for m in submitted]
    return data


def _predictions(user):
    from apps.ai_insights.models import ModelPrediction

    return [{
        'id':             str(p.pk),
        'model_name':     p.model.name if p.model_id else None,
        'model_category': p.model.category if p.model_id else None,
        'input_data':     p.input_data,
        'result':         p.result,
        'risk_score':     p.risk_score,
        'interpretation': p.interpretation,
        'notes':          p.notes,
        'created_at':     _dt(p.created_at),
        'attached_file':  _archive_path_for(p) if p.input_file else None,
    } for p in ModelPrediction.objects.filter(patient=user)
                                      .select_related('model').order_by('-created_at')]


def _notifications(user):
    from apps.notifications.models import FCMDevice, Notification

    return {
        'notifications': [{
            'id':         str(n.pk),
            'type':       n.type,
            'title':      n.title,
            'message':    n.message,
            'is_read':    n.is_read,
            'created_at': _dt(n.created_at),
        } for n in Notification.objects.filter(user=user).order_by('-created_at')],
        # Token values deliberately omitted — they are device credentials.
        'registered_devices': [{
            'registered_at': _dt(d.created_at),
            'last_seen_at':  _dt(d.updated_at),
        } for d in FCMDevice.objects.filter(user=user).order_by('-updated_at')],
    }


def _consent(user):
    """The user's own processing-consent history — explicitly in scope."""
    from apps.accounts.consent import consent_status
    from apps.accounts.models import Consent

    return {
        'current': [{
            'purpose':         row['purpose'],
            'description':     row['description'],
            'granted':         row['granted'],
            'current_version': row['current_version'],
            'granted_version': row['granted_version'],
            'granted_at':      _dt(row['granted_at']),
        } for row in consent_status(user)],
        'history': [{
            'purpose':    c.purpose,
            'version':    c.version,
            'status':     c.status,
            'granted_at': _dt(c.granted_at),
            'revoked_at': _dt(c.revoked_at),
            'recorded_at': _dt(c.created_at),
        } for c in Consent.objects.filter(user=user).order_by('created_at')],
    }


def _access_history(user):
    """
    Transparency data: who reached this user's records, and which clinicians are
    linked to them.

    Only the subject side is included. If this user is a clinician, their own
    accesses to other patients are omitted — those rows describe the other
    patient, not this one.
    """
    from apps.accounts.models import (DoctorAccessLog, EmergencyCardView,
                                      PatientDoctorRelationship)

    accesses = DoctorAccessLog.objects.filter(patient=user).select_related('actor')
    links = (PatientDoctorRelationship.objects.filter(patient=user)
             .select_related('doctor', 'doctor__doctor_profile'))

    profile = getattr(user, 'patient_profile', None)
    card_views = (EmergencyCardView.objects.filter(profile=profile)
                  if profile is not None else [])

    return {
        'clinician_access_to_my_records': [{
            'accessed_at': _dt(a.accessed_at),
            'resource':    a.resource,
            # Professional identity of the accessing clinician: the minimum
            # needed for this log to be meaningful to the subject. Falls back to
            # the label captured at access time when the account has since been
            # deleted — "someone read your records" is not an answer.
            'accessed_by': ((a.actor.get_full_name() or a.actor.username) if a.actor_id
                            else (a.actor_label or 'deleted account')),
        } for a in accesses.order_by('-accessed_at')],
        'linked_clinicians': [{
            'doctor_name': l.doctor.get_full_name() or l.doctor.username,
            'specialty':   getattr(getattr(l.doctor, 'doctor_profile', None), 'specialty', ''),
            'hospital':    l.hospital,
            'status':      l.status,
            'decided_at':  _dt(l.decided_at),
            'linked_at':   _dt(l.created_at),
        } for l in links.order_by('-created_at')],
        # Timestamps only: the stored IP hash identifies a third-party viewer.
        'emergency_card_views': [
            {'viewed_at': _dt(v.viewed_at)} for v in card_views
        ],
    }


#: category name -> (filename, builder). Order is the order in the manifest.
CATEGORIES = [
    ('account',         'user.json',            _account),
    ('medical_records', 'medical_records.json', _medical_records),
    ('conversations',   'conversations.json',   _conversations),
    ('appointments',    'appointments.json',    _appointments),
    ('insights',        'insights.json',        _insights),
    ('predictions',     'predictions.json',     _predictions),
    ('notifications',   'notifications.json',   _notifications),
    ('consent',         'consent.json',         _consent),
    ('access_history',  'access_history.json',  _access_history),
    ('rag_index',       'rag_index.json',       _rag_index),
]


# ── Attached files ────────────────────────────────────────────────────────────

def _archive_path_for(obj) -> str:
    """
    Deterministic, traversal-proof path inside the archive.

    Built from the object's UUID plus a sanitised extension — never from the
    stored filename, so a record whose name is `../../etc/passwd` cannot escape
    the archive directory or collide with another entry.
    """
    field = getattr(obj, 'file', None) or getattr(obj, 'input_file', None)
    name = getattr(field, 'name', '') or ''
    ext = ''
    if '.' in name.rsplit('/', 1)[-1]:
        raw = name.rsplit('.', 1)[-1]
        ext = '.' + ''.join(c for c in raw if c.isalnum())[:10].lower()
    folder = 'medical_records' if hasattr(obj, 'record_type') else 'predictions'
    return f'files/{folder}/{obj.pk}{ext}'


def _iter_user_files(user):
    """
    Yield (archive_path, file_field, original_name) for this user's uploads.

    Files are reached through the storage backend from querysets already
    filtered to the user, so no filesystem path is ever constructed from
    untrusted input and another user's file cannot be selected.
    """
    from apps.ai_insights.models import ModelPrediction
    from apps.medical_records.models import MedicalRecord

    for rec in MedicalRecord.objects.filter(patient=user).exclude(file=''):
        if rec.file:
            yield _archive_path_for(rec), rec.file, rec.file.name

    for pred in ModelPrediction.objects.filter(patient=user).exclude(input_file=''):
        if pred.input_file:
            yield _archive_path_for(pred), pred.input_file, pred.input_file.name

    if user.profile_picture:
        name = user.profile_picture.name
        ext = ''
        if '.' in name.rsplit('/', 1)[-1]:
            raw = name.rsplit('.', 1)[-1]
            ext = '.' + ''.join(c for c in raw if c.isalnum())[:10].lower()
        yield f'files/profile/profile_picture{ext}', user.profile_picture, name


# ── Archive assembly ──────────────────────────────────────────────────────────

def build_export(user):
    """
    Build the export archive for *user* and return (file_object, filename).

    The file object is positioned at 0 and is the caller's to close. `user` must
    already be the authenticated subject — this function has no other way to
    identify whose data to gather.
    """
    generated_at = timezone.now()
    buffer = SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES)

    categories_meta = []
    files_meta = []

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, filename, builder in CATEGORIES:
            payload = builder(user)
            archive.writestr(filename, _safe_json(payload))
            categories_meta.append({
                'category':     name,
                'file':         filename,
                'record_count': _count(payload),
            })

        for archive_path, field, original_name in _iter_user_files(user):
            try:
                size = field.size
                if size > _MAX_FILE_BYTES:
                    files_meta.append({
                        'path': None, 'original_filename': _basename(original_name),
                        'size_bytes': size, 'included': False,
                        'reason': 'exceeds per-file export limit',
                    })
                    continue
                with field.open('rb') as handle:
                    archive.writestr(archive_path, handle.read())
            except Exception as exc:
                # A missing blob (deleted from storage, ephemeral disk) must not
                # fail the whole export — the metadata still belongs to the user.
                logger.warning('export: attachment unavailable (%s)', type(exc).__name__)
                files_meta.append({
                    'path': None, 'original_filename': _basename(original_name),
                    'size_bytes': None, 'included': False,
                    'reason': 'file not available in storage',
                })
                continue

            files_meta.append({
                'path':              archive_path,
                'original_filename': _basename(original_name),
                'size_bytes':        size,
                'included':          True,
            })

        manifest = {
            'export_version': EXPORT_VERSION,
            'generated_at':   generated_at.isoformat(),
            'service':        'HealthCompass',
            'subject': {
                # Identified by the credentials the user already knows. The
                # internal numeric primary key is deliberately not disclosed.
                'username': user.username,
                'email':    user.email,
            },
            'data_categories': categories_meta,
            'files':           files_meta,
            'exclusions':      EXCLUSIONS,
            'notes': (
                'Contains special-category health data and may contain a national '
                'identifier. Store it encrypted and share it with no one you do '
                'not intend to give your full medical history. Timestamps are ISO 8601.'
            ),
        }
        archive.writestr('manifest.json', _safe_json(manifest))

    buffer.seek(0)
    logger.info('export: archive generated for user %s', user.pk)
    return buffer, f'healthcompass-export-{generated_at:%Y%m%d-%H%M%S}.zip'


def _count(payload):
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return sum(len(v) for v in payload.values() if isinstance(v, list)) or 1
    return 1


def _basename(name: str) -> str:
    return (name or '').replace('\\', '/').rsplit('/', 1)[-1]

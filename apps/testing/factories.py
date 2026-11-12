"""Small factory functions for new tests (audit T-01). Existing tests keep their
own fixtures; new ones use these so setup reads the same everywhere."""
from itertools import count

from django.contrib.auth import get_user_model

_seq = count(1)


def _unique(prefix):
    return f'{prefix}{next(_seq)}'


def make_patient(**kw):
    name = kw.pop('username', None) or _unique('patient')
    return get_user_model().objects.create_user(
        name, email=kw.pop('email', f'{name}@test.invalid'), password=kw.pop('password', 'pw'),
        role='patient', **kw)


def make_doctor(approved=True, **kw):
    name = kw.pop('username', None) or _unique('doctor')
    return get_user_model().objects.create_user(
        name, email=kw.pop('email', f'{name}@test.invalid'), password=kw.pop('password', 'pw'),
        role='doctor', is_approved=approved, **kw)


def make_record_with_labs(patient, labs=(('Hb', '140', 'g/L'),), **kw):
    from apps.medical_records.models import MedicalRecord, ParsedLabValue
    record = MedicalRecord.objects.create(patient=patient, title=kw.pop('title', 'Lab'),
                                          record_type=kw.pop('record_type', 'lab_result'), **kw)
    for name, value, unit in labs:
        ParsedLabValue.objects.create(record=record, parameter_name=name, value=value, unit=unit)
    return record


def make_share(patient, recipient, *, records=True, alerts=False, appointments=False, active=True):
    from apps.accounts.models import SharingGrant
    return SharingGrant.objects.create(
        patient=patient, recipient=recipient, can_view_records=records, can_view_alerts=alerts,
        can_view_appointments=appointments,
        status=SharingGrant.Status.ACTIVE if active else SharingGrant.Status.PENDING)

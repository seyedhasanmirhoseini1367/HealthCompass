"""
Authorisation predicates shared by the web views and the API.

Both surfaces answer the same questions — may this user read this patient's
file, may this user see cohort-level statistics — and they were answering them
differently. `population_view` was `@login_required` with no role check while
the dashboard's `monitoring` view enumerated four roles; media downloads granted
access to any `is_staff` account and recorded nothing.

Keeping the rules here means a change to who may see what is made once, and the
tests can assert the rule rather than each caller's copy of it.

Nothing here decides *clinical* questions. These are access rules only.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def can_view_population_analytics(user) -> bool:
    """
    Cohort-wide statistics: biomarker averages, alert counts, risk buckets.

    Aggregates are not anonymous by construction. With a small cohort — which is
    what this deployment has — "average creatinine among 3 patients" plus one
    known value discloses the others, and the trending list names the analytes
    those patients were tested for. So this is a research/administrative view,
    not a patient-facing one.

    Patients get their own analytics through the per-patient views; nothing here
    restricts a patient's access to their own data.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    return bool(
        getattr(user, 'is_data_scientist', False)
        or getattr(user, 'is_hospital_admin', False)
        or user.is_staff
        or user.is_superuser
    )


def resolve_media_owner(relative_path: str):
    """
    Return (owner, resource_label) for a stored media path.

    owner is the patient whose PHI the file is, or the account that owns a
    non-clinical artifact. Returns (None, None) for a path we cannot attribute —
    which is treated as "not disclosable", never as "unowned, therefore public".
    """
    from django.contrib.auth import get_user_model

    from apps.ai_insights.models import AIModel, ModelPrediction
    from apps.medical_records.models import MedicalRecord

    record = (MedicalRecord.objects.filter(file=relative_path)
              .select_related('patient').first())
    if record is not None:
        return record.patient, f'record:{record.pk}'

    prediction = (ModelPrediction.objects.filter(input_file=relative_path)
                  .select_related('patient').first())
    if prediction is not None:
        return prediction.patient, f'prediction:{prediction.pk}'

    User = get_user_model()
    owner = User.objects.filter(profile_picture=relative_path).first()
    if owner is not None:
        return owner, 'profile_picture'

    model = (AIModel.objects.filter(model_file=relative_path)
             .select_related('data_scientist').first())
    if model is not None:
        return model.data_scientist, f'ai_model:{model.pk}'

    return None, None


def doctor_has_active_link(doctor, patient) -> bool:
    """True when an ACTIVE (patient-approved) relationship exists."""
    from apps.accounts.models import PatientDoctorRelationship

    if doctor is None or patient is None or doctor.pk == patient.pk:
        return False
    if not getattr(doctor, 'is_doctor', False):
        return False
    return PatientDoctorRelationship.objects.filter(
        doctor=doctor, patient=patient,
        status=PatientDoctorRelationship.Status.ACTIVE,
    ).exists()


def log_phi_access(actor, patient, resource: str) -> None:
    """
    Append one row to the access trail.

    Deliberately best-effort at the boundary: a logging failure must not hand
    the caller a 500 in place of their file, but it must not pass silently
    either, because the trail is what a patient is entitled to ask for.
    """
    from apps.accounts.models import DoctorAccessLog

    try:
        DoctorAccessLog.objects.create(actor=actor, patient=patient, resource=resource)
    except Exception as exc:
        from healthcompass.observability import Event as OpsEvent, emit as ops_emit
        logger.error('Could not record PHI access %s by %s: %s',
                     resource, getattr(actor, 'pk', None), exc)
        ops_emit(OpsEvent.ACCESS_LOG_FAILED,
                 actor_id=getattr(actor, 'pk', None),
                 patient_id=getattr(patient, 'pk', None),
                 error_type=type(exc).__name__)


def can_access_media(user, relative_path: str) -> bool:
    """
    Decide a media download, and record it when it is someone else's PHI.

    Three ways to be allowed:
      * it is your own file — no log entry, a patient reading their own record
        is not an access event anyone needs to audit;
      * you are a doctor with an ACTIVE link to the owning patient — this used
        to be refused outright, so a doctor could read a record's parsed values
        in the dashboard but not open the PDF it came from;
      * you are staff or a superuser — previously an unconditional, unlogged
        bypass. Still allowed, because such accounts can read the same content
        through the Django admin, but no longer silent.
    """
    owner, resource = resolve_media_owner(relative_path)
    if owner is None:
        # Unattributable file. Refuse rather than fall back to a role check:
        # if we cannot say whose data it is, we cannot say who may read it.
        return False

    if owner.pk == user.pk:
        return True

    # A model artifact belongs to the data scientist who uploaded it; it is not
    # anyone's health data, so reading it is not an access event for the trail.
    is_phi = not resource.startswith('ai_model:')

    if doctor_has_active_link(user, owner):
        log_phi_access(user, owner, f'media:{resource}')
        return True

    if user.is_staff or user.is_superuser:
        if is_phi:
            log_phi_access(user, owner, f'media:{resource}')
        return True

    return False

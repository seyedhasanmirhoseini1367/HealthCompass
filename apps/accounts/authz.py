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


#: Scopes a patient can share. Named after the resources this application
#: already has, so the vocabulary on the sharing screen matches the vocabulary
#: everywhere else.
SHARE_SCOPES = ('records', 'alerts', 'appointments')


def sharing_grant(recipient, patient, scope: str):
    """
    The effective grant letting *recipient* see *patient*'s *scope*, or None.

    One query, one place. Every view, endpoint and template asks this rather
    than filtering SharingGrant themselves, because a status-and-expiry filter
    repeated at five call sites is four chances to forget the expiry half.

    Administrative authority is deliberately absent from this function. An
    administrator holds no grant, so they get None here — being staff is not a
    way to become someone's family.
    """
    from apps.accounts.models import SharingGrant

    if recipient is None or patient is None:
        return None
    if not getattr(recipient, 'is_authenticated', False):
        return None
    if recipient.pk == patient.pk:
        return None
    if scope not in SHARE_SCOPES:
        return None

    grant = (SharingGrant.objects
             .filter(recipient=recipient, patient=patient,
                     status=SharingGrant.Status.ACTIVE)
             .first())
    if grant is None or not grant.allows(scope):
        return None
    return grant


def shared_with(recipient, scope: str):
    """Every patient whose *scope* this recipient may currently see."""
    from apps.accounts.models import SharingGrant

    if not getattr(recipient, 'is_authenticated', False):
        return []
    grants = (SharingGrant.objects
              .filter(recipient=recipient, status=SharingGrant.Status.ACTIVE,
                      **{f'can_view_{scope}': True})
              .select_related('patient'))
    return [g.patient for g in grants if g.allows(scope)]


def can_view_shared_records(recipient, patient) -> bool:
    """
    Whether *recipient* may read *patient*'s records through a family share.

    Separate from `doctor_has_active_link` on purpose. A clinician's access
    needs an ACTIVE hospital-created link AND the patient's DATA_SHARING
    consent; a family share is the patient handing a specific person a specific
    key. Collapsing them would let a share satisfy a clinical rule it was never
    checked against, or make family access require a consent written about
    clinicians.

    Both are consulted by `can_access_media`, and neither widens the other.
    """
    return sharing_grant(recipient, patient, 'records') is not None


def can_revoke_grant(user, grant) -> bool:
    """
    Who may end a share.

    The patient, always — it is their data and their decision.

    An administrator, deliberately: a recipient can be abusive, and the subject
    may be the last person able to act. This is asymmetric on purpose. Revoking
    removes access and can be justified for someone else's safety; creating it
    cannot, so `can_create_grant` has no administrative branch at all.
    """
    if grant is None or not getattr(user, 'is_authenticated', False):
        return False
    if grant.patient_id == user.pk:
        return True
    return bool(user.is_staff or user.is_superuser)


def can_create_grant(user, patient) -> bool:
    """
    Only the patient, for their own data. No administrative branch exists.

    An administrator creating a share on someone's behalf is impersonating
    consent, and there is no operational need that outweighs that: if a patient
    wants to share, they can.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    return user.pk == getattr(patient, 'pk', None)


def can_inspect_grant_metadata(user) -> bool:
    """
    Read WHO shared with WHOM, and when — never the records behind it.

    Compliance needs to answer "who has access to this patient's data" without
    that question becoming a way to read the data. `can_view_shared_records`
    has no administrative branch, so holding this changes nothing about what an
    administrator can actually see.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    return bool(user.is_staff or user.is_superuser)


def can_correct_clinical_value(user) -> bool:
    """
    Who may append a correction to an extracted lab value.

    Platform administration, and only that. The reasoning is not that
    administrators are clinically qualified — they are not, and a correction is
    explicitly not a clinical judgement. It is an assertion that the extraction
    misread the source document, which is a data-integrity question about this
    system's own parsing.

    Deliberately NOT the patient, and not their doctor:

      * the patient owns the data but not its evidential integrity. A subject
        able to rewrite their own lab history destroys the value of the record
        for everyone who relies on it, including themselves.
      * a doctor's authority here is to READ a linked patient's records under
        consent. Writing to them is a different power, and consent to be read is
        not consent to be edited.

    This keeps administrative authority separate from clinical-data ownership:
    the administrator can fix what the parser got wrong and still cannot decide
    anything clinical, because a correction records what the document said, not
    what it means.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    return bool(user.is_staff or user.is_superuser)


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
    """
    True when an ACTIVE (patient-approved) relationship exists AND the patient
    still consents to sharing records with clinicians.

    Two independent conditions, deliberately both required:

      * the per-doctor relationship, which the patient approves individually;
      * ConsentPurpose.DATA_SHARING, which is the patient's blanket permission
        for this category of processing and acts as a master switch over every
        link at once.

    DATA_SHARING was defined and described but checked nowhere, so a patient
    could revoke "Sharing my records with linked clinicians" on the consent page
    and every linked doctor carried on reading. A consent control that changes
    nothing is worse than an absent one: it tells the data subject they have
    exercised a right they have not.

    The check lives here, beside the relationship test, so the web views and the
    media path cannot answer this question differently.
    """
    from apps.accounts.consent import has_consent
    from apps.accounts.models import ConsentPurpose, PatientDoctorRelationship

    if doctor is None or patient is None or doctor.pk == patient.pk:
        return False
    if not getattr(doctor, 'is_doctor', False):
        return False
    if not PatientDoctorRelationship.objects.filter(
        doctor=doctor, patient=patient,
        status=PatientDoctorRelationship.Status.ACTIVE,
    ).exists():
        return False

    return has_consent(patient, ConsentPurpose.DATA_SHARING)


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

    # A family share, which is a different authority from a clinical link and
    # is checked separately so neither can widen the other. Logged the same way:
    # the patient's access trail must show every person who opened their file,
    # not only the clinicians.
    if is_phi and can_view_shared_records(user, owner):
        log_phi_access(user, owner, f'media:{resource}')
        return True

    if user.is_staff or user.is_superuser:
        if is_phi:
            log_phi_access(user, owner, f'media:{resource}')
        return True

    return False

"""Patient control over which clinicians can read their records."""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST


from ..models import (PatientDoctorRelationship, DoctorAccessLog)

logger = logging.getLogger(__name__)


# ── Patient control over who can read their records ──────────────────────────
#
# None of this existed. A hospital admin created a link and the doctor could
# read the records immediately; the patient was told afterwards and had no way
# to stop it. `remove_link` in the dashboard is scoped to linked_by=<the admin
# who created it>, so not even a different admin could revoke one. For a
# GDPR-scoped health product, a data subject who cannot terminate a third
# party's access to their own records is a compliance problem, not a UX gap.

@login_required
def my_doctors(request):
    """Every access request and grant on this patient's records."""
    links = (PatientDoctorRelationship.objects
             .filter(patient=request.user)
             .select_related('doctor', 'doctor__doctor_profile')
             .order_by('-created_at'))
    return render(request, 'accounts/my_doctors.html', {
        'links':   links,
        'Status':  PatientDoctorRelationship.Status,
    })


@login_required
@require_POST
def approve_doctor_access(request, pk):
    """Grant a pending request. POST only — this changes who can read PHI."""
    link = get_object_or_404(PatientDoctorRelationship, pk=pk, patient=request.user)

    if link.status == PatientDoctorRelationship.Status.REVOKED:
        messages.error(request, 'This access was revoked and cannot be re-approved '
                                'here. Ask your clinic to send a new request.')
        return redirect('accounts:my_doctors')

    # Refuse while clinician sharing is switched off.
    #
    # DATA_SHARING is a master switch: turning it off closes every link at once
    # without touching their status. Granting it as a side effect of approving
    # ONE doctor would silently restore every other doctor the patient had
    # deliberately cut off — one affirmative act about one person reopening
    # three they never mentioned.
    #
    # Only an explicit withdrawal blocks here. A patient approving their first
    # doctor has never been asked about DATA_SHARING, and that decision is
    # exactly what this action expresses, so it is granted below as before.
    from ..consent import was_revoked
    from ..models import ConsentPurpose
    if was_revoked(request.user, ConsentPurpose.DATA_SHARING):
        messages.error(
            request,
            'Sharing with clinicians is currently switched off, so this request '
            'cannot be approved. Turn "Sharing my records with linked clinicians" '
            'back on in Privacy & Consent first — note that doing so restores '
            'access for every clinician whose link is still active.')
        return redirect('accounts:consent')

    link.status = PatientDoctorRelationship.Status.ACTIVE
    link.decided_at = timezone.now()
    link.save(update_fields=['status', 'decided_at'])

    # Approving a link is an affirmative decision to share records with a
    # clinician, so it records the DATA_SHARING consent that authz now requires.
    # Without this the patient would approve a doctor and the doctor would still
    # be refused, with nothing on either screen explaining why.
    #
    # It is a master switch, not a per-doctor flag: revoking DATA_SHARING on the
    # consent page closes every link at once, and approving a second doctor
    # while it is already granted is idempotent.
    from ..consent import grant_consent
    from ..models import ConsentPurpose
    grant_consent(request.user, ConsentPurpose.DATA_SHARING)

    # The access log is the record patients can later ask to see; a grant is as
    # much a part of that history as a read.
    DoctorAccessLog.objects.create(
        actor=request.user, patient=request.user,
        resource=f'access_granted:doctor:{link.doctor_id}')
    logger.info('Patient %s granted record access to doctor %s',
                request.user.pk, link.doctor_id)

    messages.success(request, f'Dr. {link.doctor.get_full_name() or link.doctor.username} '
                              f'can now view your records.')
    return redirect('accounts:my_doctors')


@login_required
@require_POST
def revoke_doctor_access(request, pk):
    """
    Withdraw a doctor's access. POST only.

    The row is kept rather than deleted: who had access, and when it ended, is
    exactly the history the audit trail exists to preserve.
    """
    link = get_object_or_404(PatientDoctorRelationship, pk=pk, patient=request.user)

    link.status = PatientDoctorRelationship.Status.REVOKED
    link.decided_at = timezone.now()
    link.save(update_fields=['status', 'decided_at'])

    DoctorAccessLog.objects.create(
        actor=request.user, patient=request.user,
        resource=f'access_revoked:doctor:{link.doctor_id}')
    logger.info('Patient %s revoked record access from doctor %s',
                request.user.pk, link.doctor_id)

    messages.success(request, 'Access revoked. That doctor can no longer view your records.')
    return redirect('accounts:my_doctors')

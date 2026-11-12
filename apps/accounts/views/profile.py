"""Profile, consent settings, data export, password and account deletion."""
import logging

from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django_ratelimit.decorators import ratelimit


from ..models import (PatientProfile, DoctorProfile, DataScientistProfile,
                     HospitalAdminProfile)
from ..forms import (ProfileForm, PasswordChangeForm,
                    PatientProfileForm, DoctorProfileForm, DataScientistProfileForm)

logger = logging.getLogger(__name__)


@login_required
def profile_view(request):
    user = request.user
    profile = None
    if user.is_patient:
        profile, _ = PatientProfile.objects.get_or_create(user=user)
    elif user.is_doctor:
        profile, _ = DoctorProfile.objects.get_or_create(user=user)
    elif user.is_data_scientist:
        profile, _ = DataScientistProfile.objects.get_or_create(user=user)
    elif user.is_hospital_admin:
        profile, _ = HospitalAdminProfile.objects.get_or_create(user=user, defaults={'hospital_name': ''})
    return render(request, "accounts/profile.html", {"profile": profile})


@login_required
def consent_settings(request):
    """
    Privacy & Consent page: view current status, grant or withdraw each purpose.

    Each purpose is its own control — there is deliberately no single
    "accept everything" action, because the purposes describe genuinely
    different processing and a user must be able to accept one and decline
    another.
    """
    from ..consent import consent_history, consent_status, grant_consent, revoke_consent
    from ..models import ConsentPurpose

    if request.method == "POST":
        purpose = request.POST.get("purpose", "")
        action  = request.POST.get("action", "")
        if purpose not in ConsentPurpose.values:
            messages.error(request, "Unknown consent option.")
            return redirect("accounts:consent")

        label = dict(ConsentPurpose.choices)[purpose]
        if action == "grant":
            grant_consent(request.user, purpose)
            messages.success(request, f"Consent granted: {label}.")
        elif action == "revoke":
            revoke_consent(request.user, purpose)
            messages.success(request, f"Consent withdrawn: {label}.")
        else:
            messages.error(request, "Unknown action.")
        return redirect("accounts:consent")

    return render(request, "accounts/consent.html", {
        "consents": consent_status(request.user),
        "history":  consent_history(request.user)[:50],
    })


@login_required
@ratelimit(key='user', rate='5/h', method='POST', block=False)
def data_export(request):
    """
    Download every piece of personal data HealthCompass holds for the caller.

    POST-only for the download itself so it is not triggered by a link preview
    or prefetch. The subject is always request.user — there is no parameter that
    could name a different account.
    """
    from django.http import FileResponse
    from ..export import EXPORT_VERSION, EXCLUSIONS, CATEGORIES, build_export

    if request.method == "POST":
        if getattr(request, 'limited', False):
            messages.error(request, "Too many export requests. Please try again later.")
            return redirect("accounts:data_export")
        try:
            archive, filename = build_export(request.user)
        except Exception:
            logger.exception('Data export failed for user %s', request.user.pk)
            messages.error(request, "Sorry, the export could not be generated. Please try again.")
            return redirect("accounts:data_export")

        response = FileResponse(archive, as_attachment=True, filename=filename,
                                content_type='application/zip')
        # Never let a proxy or the browser retain a copy of a health archive.
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
        return response

    return render(request, "accounts/data_export.html", {
        'export_version': EXPORT_VERSION,
        'categories':     [name for name, _f, _b in CATEGORIES],
        'exclusions':     EXCLUSIONS,
    })


def _profile_form_for(user, data=None):
    """Return (ProfileFormClass, profile_instance) for the user's role, or (None, None)."""
    if user.is_patient:
        instance, _ = PatientProfile.objects.get_or_create(user=user)
        return PatientProfileForm, instance
    if user.is_doctor:
        instance, _ = DoctorProfile.objects.get_or_create(user=user)
        return DoctorProfileForm, instance
    if user.is_data_scientist:
        instance, _ = DataScientistProfile.objects.get_or_create(user=user)
        return DataScientistProfileForm, instance
    return None, None


@login_required
def profile_edit(request):
    user = request.user
    ProfileFormClass, profile_instance = _profile_form_for(user)

    if request.method == 'POST':
        user_form    = ProfileForm(request.POST, request.FILES, instance=user)
        profile_form = (ProfileFormClass(request.POST, instance=profile_instance)
                        if ProfileFormClass else None)

        user_valid    = user_form.is_valid()
        profile_valid = profile_form.is_valid() if profile_form else True

        if user_valid and profile_valid:
            user_form.save()
            if profile_form:
                profile_form.save()
            messages.success(request, 'Profile updated.')
            return redirect('accounts:profile')
        messages.error(request, 'Please correct the errors below.')
    else:
        user_form    = ProfileForm(instance=user)
        profile_form = (ProfileFormClass(instance=profile_instance)
                        if ProfileFormClass else None)

    return render(request, 'accounts/profile_edit.html', {
        'form':         user_form,
        'profile_form': profile_form,
    })


@login_required
def change_password(request):
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            from ..auth_service import revoke_all_tokens
            revoke_all_tokens(user)
            messages.success(request, "Password changed successfully.")
            return redirect("accounts:profile")
        messages.error(request, "Please correct the errors below.")
    else:
        form = PasswordChangeForm(request.user)
    return render(request, "accounts/change_password.html", {"form": form})


@login_required
def delete_account(request):
    """Password accounts confirm with the password. Accounts without one
    (Google sign-in) type their email and confirm through an emailed link, so
    they can delete themselves too (audit SEC-13)."""
    user = request.user
    has_password = user.has_usable_password()
    if request.method == "POST":
        if has_password:
            if user.check_password(request.POST.get("password", "")):
                logout(request)
                from ..auth_service import revoke_all_tokens
                from ..services import purge_user_data
                revoke_all_tokens(user)
                purge_user_data(user)
                messages.success(request, "Your account and all associated data have been permanently deleted.")
                return redirect("home")
            messages.error(request, "Incorrect password. Account not deleted.")
        else:
            typed = (request.POST.get("confirm_email") or "").strip().lower()
            if typed and typed == (user.email or "").lower():
                from ..security_views import send_delete_link
                send_delete_link(request, user)
                messages.success(request, f"We sent a confirmation link to {user.email}. "
                                          "Open it within 30 minutes to delete your account.")
                return redirect("accounts:profile")
            messages.error(request, "That does not match your account email. Account not deleted.")
    return render(request, "accounts/delete_account.html", {"has_password": has_password})


# Readable names for the resource codes in DoctorAccessLog.
_ACCESS_LABELS = (
    ('share_granted', 'Sharing started'), ('share_offered', 'Sharing offered'),
    ('share_revoked', 'Sharing stopped'), ('share_declined', 'Sharing declined'),
    ('access_granted', 'Clinician access approved'), ('access_revoked', 'Clinician access revoked'),
    ('assistant', 'Assistant question about your records'), ('record', 'Opened a record'),
    ('file', 'Opened a file'), ('export', 'Data export'),
)


def _describe_access(resource: str) -> str:
    for prefix, label in _ACCESS_LABELS:
        if resource.startswith(prefix):
            return label
    return 'Viewed your information'


@login_required
def access_history(request):
    """Who has opened this patient's data, newest first (audit UX-08).
    Only rows about the signed-in user; never anyone else's."""
    from django.core.paginator import Paginator

    from ..models import DoctorAccessLog

    rows = (DoctorAccessLog.objects.filter(patient=request.user)
            .select_related('actor').order_by('-accessed_at'))
    page = Paginator(rows, 25).get_page(request.GET.get('page'))
    entries = [{'when': r.accessed_at,
                'who': ('You' if r.actor_id == request.user.pk
                        else (r.actor.get_full_name() or r.actor.username) if r.actor else (r.actor_label or 'A deleted account')),
                'what': _describe_access(r.resource or '')} for r in page]
    return render(request, 'accounts/access_history.html', {'page': page, 'entries': entries})

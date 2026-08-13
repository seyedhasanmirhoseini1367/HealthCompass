import hashlib
import logging

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetView
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from allauth.socialaccount.views import SignupView as BaseSocialSignupView
from django_ratelimit.decorators import ratelimit

from .models import (PatientProfile, DoctorProfile, DataScientistProfile,
                     HospitalAdminProfile, EmergencyCardView,
                     PatientDoctorRelationship, DoctorAccessLog)
from .forms import (RegisterForm, LoginForm, ProfileForm, PasswordChangeForm,
                    PatientProfileForm, DoctorProfileForm, DataScientistProfileForm)

logger = logging.getLogger(__name__)


def _email_is_verified(user, email: str) -> bool:
    """
    Has this account proven it owns this address?

    allauth records confirmation in EmailAddress.verified. A local account with
    no EmailAddress row, or one that is unconfirmed, has proven nothing — see
    the pre-hijack note in AutoCompleteSocialSignup.get().

    Fails CLOSED: if allauth's model cannot be read for any reason, the answer
    is "not verified". An error here must not become a free account link.
    """
    try:
        from allauth.account.models import EmailAddress
        return EmailAddress.objects.filter(
            user=user, email__iexact=email, verified=True).exists()
    except Exception:
        logger.exception('Could not determine email verification state; '
                         'treating as unverified')
        return False


class AutoCompleteSocialSignup(BaseSocialSignupView):
    """Skip the signup form — auto-complete without asking for a role.
    If the email already exists, connect the Google account to that user.
    Role defaults to patient; admin can change it later."""

    def get(self, request, *args, **kwargs):
        from django.contrib.auth import get_user_model
        from allauth.socialaccount.internal.flows.signup import (
            complete_social_signup, clear_pending_signup,
        )

        sociallogin = self.sociallogin
        User = get_user_model()

        # Case 1: email already registered — connect Google to that account,
        # but ONLY if that account has proven it owns the address.
        #
        # Account pre-hijack, which this guard closes:
        #   1. Attacker registers locally as victim@gmail.com with a password
        #      they choose. ACCOUNT_EMAIL_VERIFICATION is 'none', so no
        #      confirmation mail is ever sent and the victim never learns.
        #   2. Victim later signs in with Google. allauth matches the address
        #      and, unguarded, connects the victim's Google identity to the
        #      ATTACKER's account.
        #   3. Victim uploads medical records into an account the attacker can
        #      still log into with the original password.
        #
        # Google's assertion proves the VICTIM owns the mailbox; an unverified
        # local account has proven nothing. So an unverified match is the
        # suspicious party and must not receive the identity. Refusing degrades
        # to a denial of service against the squatted address — recoverable via
        # password reset, which itself proves ownership — instead of a silent
        # account takeover.
        if sociallogin.email_addresses:
            email = sociallogin.email_addresses[0].email
            try:
                existing_user = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                existing_user = None
            except User.MultipleObjectsReturned:
                # Case-variant duplicates can exist because email uniqueness is
                # case-sensitive while lookup is not. Refuse rather than pick.
                logger.error('Social login matched multiple accounts for one address')
                messages.error(request, 'Could not complete Google sign-in. '
                                        'Please contact support.')
                return redirect('/accounts/login/')

            if existing_user is not None:
                if not _email_is_verified(existing_user, email):
                    logger.warning(
                        'Refused to connect a social identity to an unverified '
                        'local account (user pk=%s)', existing_user.pk)
                    messages.error(
                        request,
                        'An account with this email already exists but the address '
                        'has not been confirmed, so it cannot be linked to Google. '
                        'Sign in with your password, or use “Forgot password” to '
                        'confirm you own this address.')
                    return redirect('/accounts/login/')

                clear_pending_signup(request)
                sociallogin.connect(request, existing_user)
                return complete_social_signup(request, sociallogin)

        # Case 2: brand new user — create account as patient
        try:
            from allauth.socialaccount.adapter import get_adapter
            clear_pending_signup(request)
            get_adapter().save_user(request, sociallogin, form=None)
            return complete_social_signup(request, sociallogin)
        except Exception as e:
            logger.warning('Social auto-signup failed: %s', e)
            messages.error(request, 'Could not complete Google sign-in. Please try again.')
            return redirect('/accounts/login/')


class SafePasswordResetView(PasswordResetView):
    """Wraps Django's PasswordResetView — always redirects to done page even if email fails.

    Rate limited by IP: each POST sends an email, so an unthrottled endpoint lets
    anyone use the app as a spam relay and probe which addresses are registered.
    """
    template_name = 'accounts/password_reset.html'
    email_template_name = 'accounts/email/password_reset_email.txt'
    subject_template_name = 'accounts/email/password_reset_subject.txt'
    success_url = reverse_lazy('accounts:password_reset_done')

    # block=False rather than block=True: django-ratelimit's blocking mode raises
    # Ratelimited, a PermissionDenied subclass, which Django renders as 403.
    # Rate limiting should say 429 so clients can back off correctly.
    @method_decorator(
        ratelimit(key='ip', rate=settings.RATELIMIT_PASSWORD_RESET, method='POST', block=False)
    )
    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return render(request, self.template_name,
                          {'form': self.get_form()}, status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except Exception as e:
            logger.error('Password reset email failed: %s', e)
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(self.get_success_url())


@ratelimit(key='ip', rate=settings.RATELIMIT_REGISTER, method='POST', block=False)
def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    if getattr(request, 'limited', False):
        messages.error(request, "Too many sign-up attempts. Please try again later.")
        return render(request, "accounts/register.html", {"form": RegisterForm()}, status=429)
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            PatientProfile.objects.create(user=user)

            # Three backends are configured, so login() cannot infer which one
            # authenticated this user and raises ValueError if simply handed the
            # object. Re-authenticating with the credentials just submitted is
            # the backend-safe route: it walks AUTHENTICATION_BACKENDS, sets
            # user.backend, and still enforces every backend check (including
            # user_can_authenticate) rather than asserting a login that the
            # auth stack never actually approved.
            auth_user = authenticate(
                request,
                username=user.username,
                password=form.cleaned_data["password1"],
            )
            if auth_user is None:
                # Account exists but could not be authenticated — send them to
                # the normal login flow rather than failing the whole request.
                logger.warning('Post-registration authenticate() failed for %s', user.pk)
                messages.success(request, "Your account was created. Please log in.")
                return redirect("accounts:login")

            login(request, auth_user)
            messages.success(request, f"Welcome to HealthCompass, {auth_user.get_full_name() or auth_user.username}!")
            return redirect("dashboard:home")
        messages.error(request, "Please correct the errors below.")
    else:
        form = RegisterForm()
    return render(request, "accounts/register.html", {"form": form})


@ratelimit(key='ip', rate=settings.RATELIMIT_LOGIN, method='POST', block=False)
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    # Keyed by IP rather than username: an attacker controls the username field,
    # so a per-username limit is trivially sidestepped and also lets one attacker
    # lock a known victim out of their own account.
    if getattr(request, 'limited', False):
        messages.error(request, "Too many login attempts. Please wait a minute and try again.")
        return render(request, "accounts/login.html", {"form": LoginForm(request)}, status=429)
    if request.method == "POST":
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if not user.is_approved:
                messages.warning(request, "Your account is pending admin approval.")
                return redirect("accounts:login")
            login(request, user)
            messages.success(request, f"Welcome back, {user.username}!")
            next_url = request.GET.get("next", "")
            from django.utils.http import url_has_allowed_host_and_scheme
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect("dashboard:home")
        messages.error(request, "Invalid username or password.")
    else:
        form = LoginForm(request)
    return render(request, "accounts/login.html", {"form": form})


@login_required
def logout_view(request):
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("home")


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
    from .consent import consent_history, consent_status, grant_consent, revoke_consent
    from .models import ConsentPurpose

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
    from .export import EXPORT_VERSION, EXCLUSIONS, CATEGORIES, build_export

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
            messages.success(request, "Password changed successfully.")
            return redirect("accounts:profile")
        messages.error(request, "Please correct the errors below.")
    else:
        form = PasswordChangeForm(request.user)
    return render(request, "accounts/change_password.html", {"form": form})



# ── Emergency card ────────────────────────────────────────────────────────────

def _get_client_ip(request) -> str:
    """
    The client address, taken from REMOTE_ADDR.

    X-Forwarded-For is NOT consulted. Its first element is whatever the client
    put there — proxies append, they do not overwrite — so trusting it let any
    caller send a fresh value per request and:

      * bypass the emergency-card rate limit entirely (30/min became unlimited),
        enabling brute-force enumeration of emergency_token UUIDs;
      * poison EmergencyCardView.ip_hash, destroying the audit trail's value.

    django-ratelimit's key='ip' already uses REMOTE_ADDR for exactly this
    reason, so the hand-rolled helper was strictly weaker than the library
    already in the project.

    If a real proxy is ever terminated in front of this app, the correct fix is
    to parse XFF from the RIGHT, trusting only as many hops as are actually
    deployed — not to read element zero.
    """
    return request.META.get('REMOTE_ADDR', '') or ''

def emergency_card(request):
    """Patient's own emergency card view with QR code and recent access summary."""
    import base64, io
    import qrcode
    from django.conf import settings as s
    from django.utils import timezone
    from datetime import timedelta

    profile, _ = PatientProfile.objects.get_or_create(user=request.user)
    site_url   = getattr(s, 'SITE_URL', request.build_absolute_uri('/').rstrip('/'))
    public_url = f'{site_url}/accounts/emergency/{profile.emergency_token}/'

    qr = qrcode.QRCode(box_size=5, border=3,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(public_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    since_30d    = timezone.now() - timedelta(days=30)
    recent_views = EmergencyCardView.objects.filter(
        profile=profile, viewed_at__gte=since_30d
    ).count()

    return render(request, 'accounts/emergency_card.html', {
        'profile':      profile,
        'qr_b64':       qr_b64,
        'public_url':   public_url,
        'recent_views': recent_views,
    })


def emergency_card_public(request, token):
    """No-login public emergency card — scannable by paramedics or doctors.

    Rate-limited (30 req/min per IP) and logged to EmergencyCardView.
    Returns 404 if the patient has disabled their card.
    """
    ip      = _get_client_ip(request)
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()

    rate_key = f'ec_rl:{ip_hash[:16]}'
    hits     = cache.get(rate_key, 0)
    if hits >= 30:
        return HttpResponse('Too many requests. Please wait a minute.', status=429)
    cache.set(rate_key, hits + 1, 60)

    profile = get_object_or_404(PatientProfile, emergency_token=token)
    if not profile.emergency_card_enabled:
        raise Http404

    EmergencyCardView.objects.create(profile=profile, ip_hash=ip_hash)

    return render(request, 'accounts/emergency_card_public.html', {
        'profile': profile,
        'patient': profile.user,
    })


@login_required
def revoke_emergency_token(request):
    """POST only: regenerate the emergency card token, invalidating old links."""
    if request.method == 'POST':
        profile, _ = PatientProfile.objects.get_or_create(user=request.user)
        profile.regenerate_emergency_token()
        messages.success(request, 'Emergency card link revoked. A new link has been generated.')
    return redirect('accounts:emergency_card')


@login_required
def toggle_emergency_card(request):
    """POST only: flip emergency_card_enabled on the patient's profile."""
    if request.method == 'POST':
        profile, _ = PatientProfile.objects.get_or_create(user=request.user)
        profile.emergency_card_enabled = not profile.emergency_card_enabled
        profile.save(update_fields=['emergency_card_enabled'])
        state = 'enabled' if profile.emergency_card_enabled else 'disabled'
        messages.success(request, f'Emergency card {state}.')
    return redirect('accounts:emergency_card')


@login_required
def delete_account(request):
    if request.method == "POST":
        password = request.POST.get("password", "")
        user = request.user
        if user.check_password(password):
            logout(request)
            from .services import purge_user_data
            purge_user_data(user)
            messages.success(request, "Your account and all associated data have been permanently deleted.")
            return redirect("home")
        messages.error(request, "Incorrect password. Account not deleted.")
    return render(request, "accounts/delete_account.html")


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

    link.status = PatientDoctorRelationship.Status.ACTIVE
    link.decided_at = timezone.now()
    link.save(update_fields=['status', 'decided_at'])

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

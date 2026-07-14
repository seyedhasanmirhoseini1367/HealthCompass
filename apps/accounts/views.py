import hashlib
import logging

from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetView
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from allauth.socialaccount.views import SignupView as BaseSocialSignupView

from .models import (PatientProfile, DoctorProfile, DataScientistProfile,
                     HospitalAdminProfile, EmergencyCardView)
from .forms import (RegisterForm, LoginForm, ProfileForm, PasswordChangeForm,
                    PatientProfileForm, DoctorProfileForm, DataScientistProfileForm)

logger = logging.getLogger(__name__)


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

        # Case 1: email already registered — connect Google to that account
        if sociallogin.email_addresses:
            email = sociallogin.email_addresses[0].email
            try:
                existing_user = User.objects.get(email__iexact=email)
                clear_pending_signup(request)
                sociallogin.connect(request, existing_user)
                return complete_social_signup(request, sociallogin)
            except User.DoesNotExist:
                pass

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
    """Wraps Django's PasswordResetView — always redirects to done page even if email fails."""
    template_name = 'accounts/password_reset.html'
    email_template_name = 'accounts/email/password_reset_email.txt'
    subject_template_name = 'accounts/email/password_reset_subject.txt'
    success_url = reverse_lazy('accounts:password_reset_done')

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except Exception as e:
            logger.error('Password reset email failed: %s', e)
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(self.get_success_url())


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            PatientProfile.objects.create(user=user)
            login(request, user)
            messages.success(request, f"Welcome to HealthCompass, {user.get_full_name() or user.username}!")
            return redirect("dashboard:home")
        messages.error(request, "Please correct the errors below.")
    else:
        form = RegisterForm()
    return render(request, "accounts/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
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
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR', '')


@login_required
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

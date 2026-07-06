import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.views import PasswordResetView
from django.urls import reverse_lazy
from .models import PatientProfile, DoctorProfile, DataScientistProfile, HospitalAdminProfile
from .forms import RegisterForm, LoginForm, ProfileForm, PasswordChangeForm

logger = logging.getLogger(__name__)


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
            # Still redirect to done — never reveal whether email was sent
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
            next_url = request.GET.get("next") or "dashboard:home"
            return redirect(next_url)
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
def profile_edit(request):
    user = request.user
    form = ProfileForm(request.POST or None, request.FILES or None, instance=user)
    if request.method == "POST" and form.is_valid():
        form.save()
        # Save role-specific profile fields
        if user.is_patient:
            p, _ = PatientProfile.objects.get_or_create(user=user)
            p.blood_type = request.POST.get("blood_type", p.blood_type)
            p.allergies = request.POST.get("allergies", p.allergies)
            p.emergency_contact_name = request.POST.get("emergency_contact_name", p.emergency_contact_name)
            p.emergency_contact_phone = request.POST.get("emergency_contact_phone", p.emergency_contact_phone)
            p.save()
        elif user.is_doctor:
            p, _ = DoctorProfile.objects.get_or_create(user=user)
            p.specialty = request.POST.get("specialty", p.specialty)
            p.license_number = request.POST.get("license_number", p.license_number)
            p.hospital = request.POST.get("hospital", p.hospital)
            p.department = request.POST.get("department", p.department)
            p.save()
        elif user.is_data_scientist:
            p, _ = DataScientistProfile.objects.get_or_create(user=user)
            p.institution = request.POST.get("institution", p.institution)
            p.research_area = request.POST.get("research_area", p.research_area)
            p.save()
        messages.success(request, "Profile updated.")
        return redirect("accounts:profile")
    return render(request, "accounts/profile_edit.html", {"form": form})


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

@login_required
def emergency_card(request):
    """Patient's own emergency card view with QR code."""
    import base64, io
    import qrcode
    from django.conf import settings as s

    profile, _ = PatientProfile.objects.get_or_create(user=request.user)
    site_url = getattr(s, 'SITE_URL', request.build_absolute_uri('/').rstrip('/'))
    public_url = f'{site_url}/accounts/emergency/{profile.emergency_token}/'

    # Generate QR code as base64 PNG
    qr = qrcode.QRCode(box_size=5, border=3,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(public_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    medications = []

    return render(request, 'accounts/emergency_card.html', {
        'profile':     profile,
        'medications': medications,
        'qr_b64':      qr_b64,
        'public_url':  public_url,
    })


def emergency_card_public(request, token):
    """No-login public emergency card — scannable by paramedics or doctors."""
    profile = get_object_or_404(PatientProfile, emergency_token=token)
    user = profile.user
    medications = []

    return render(request, 'accounts/emergency_card_public.html', {
        'profile':     profile,
        'patient':     user,
        'medications': medications,
    })


@login_required
def delete_account(request):
    if request.method == "POST":
        password = request.POST.get("password", "")
        user = request.user
        if user.check_password(password):
            logout(request)
            user.delete()
            messages.success(request, "Your account has been permanently deleted.")
            return redirect("home")
        messages.error(request, "Incorrect password. Account not deleted.")
    return render(request, "accounts/delete_account.html")

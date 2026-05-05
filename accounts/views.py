from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from .models import CustomUser, PatientProfile, DoctorProfile, DataScientistProfile, HospitalAdminProfile
from .forms import RegisterForm, LoginForm, ProfileForm, PasswordChangeForm


ROLES_REQUIRING_APPROVAL = {
    CustomUser.Role.DOCTOR,
    CustomUser.Role.DATA_SCIENTIST,
    CustomUser.Role.HOSPITAL_ADMIN,
}


def _notify_admin_new_registration(user):
    admin_email = getattr(settings, 'ADMIN_NOTIFICATION_EMAIL', settings.EMAIL_HOST_USER)
    if not admin_email:
        return
    send_mail(
        subject=f'[HealthCompass] New {user.get_role_display()} registration — {user.get_full_name() or user.username}',
        message=(
            f'A new user has registered and requires approval.\n\n'
            f'Name:     {user.get_full_name() or "—"}\n'
            f'Username: {user.username}\n'
            f'Email:    {user.email}\n'
            f'Role:     {user.get_role_display()}\n\n'
            f'Approve or reject at: {settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else "your admin panel"}/admin/accounts/customuser/{user.pk}/change/'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[admin_email],
        fail_silently=True,
    )


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            role = form.cleaned_data["role"]
            needs_approval = role in ROLES_REQUIRING_APPROVAL
            if needs_approval:
                user.is_approved = False
            user.save()
            if role == CustomUser.Role.PATIENT:
                PatientProfile.objects.create(user=user)
            elif role == CustomUser.Role.DOCTOR:
                DoctorProfile.objects.create(user=user)
            elif role == CustomUser.Role.DATA_SCIENTIST:
                DataScientistProfile.objects.create(user=user)
            elif role == CustomUser.Role.HOSPITAL_ADMIN:
                HospitalAdminProfile.objects.create(user=user, hospital_name="")
            if needs_approval:
                _notify_admin_new_registration(user)
                messages.info(request, "Account created. An admin will review your registration — you'll receive an email once approved.")
                return redirect("accounts:login")
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

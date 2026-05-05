from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.conf import settings
import os
from .models import CustomUser, PatientProfile, DoctorProfile, DataScientistProfile, HospitalAdminProfile
from .forms import RegisterForm, LoginForm, ProfileForm, PasswordChangeForm


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            role = form.cleaned_data["role"]
            if role == CustomUser.Role.DATA_SCIENTIST:
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
            if role != CustomUser.Role.DATA_SCIENTIST:
                login(request, user)
                messages.success(request, f"Welcome to HealthCompass, {user.username}!")
                return redirect("dashboard:home")
            else:
                messages.info(request, "Account created. Awaiting admin approval.")
                return redirect("accounts:login")
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
def media_debug(request):
    media_root = str(settings.MEDIA_ROOT)
    user = request.user
    pic_field = str(user.profile_picture) if user.profile_picture else None
    pic_url = user.profile_picture.url if user.profile_picture else None

    media_exists = os.path.isdir(media_root)
    media_writable = os.access(media_root, os.W_OK) if media_exists else False

    files_on_disk = []
    profile_pics_dir = os.path.join(media_root, 'profile_pics')
    if os.path.isdir(profile_pics_dir):
        files_on_disk = os.listdir(profile_pics_dir)

    actual_file_exists = False
    if pic_field:
        actual_file_exists = os.path.isfile(os.path.join(media_root, pic_field))

    return JsonResponse({
        'MEDIA_ROOT': media_root,
        'MEDIA_URL': settings.MEDIA_URL,
        'media_dir_exists': media_exists,
        'media_dir_writable': media_writable,
        'profile_picture_field': pic_field,
        'profile_picture_url': pic_url,
        'actual_file_exists_on_disk': actual_file_exists,
        'files_in_profile_pics_dir': files_on_disk,
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

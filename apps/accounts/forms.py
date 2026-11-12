import re

from django import forms
from django.contrib.auth.forms import (UserCreationForm, AuthenticationForm,
                                        PasswordChangeForm as DjangoPasswordChangeForm)
from .models import CustomUser, PatientProfile, DoctorProfile, DataScientistProfile

_FC = {'class': 'form-control'}
from .validators import PHONE_RE as _PHONE_RE, validate_phone  # noqa: E402


class RegisterForm(UserCreationForm):
    email      = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=50, required=False)
    last_name  = forms.CharField(max_length=50, required=False)

    class Meta:
        model  = CustomUser
        fields = ["username", "first_name", "last_name", "email", "password1", "password2"]

    def validate_unique(self):
        """With verification on, a taken email must not surface as a form error
        (audit SEC-14); auth_service.register_or_notify handles it instead."""
        from django.core.exceptions import ValidationError
        from .auth_service import verification_required
        exclude = self._get_validation_exclusions()
        if verification_required():
            exclude.add('email')
        try:
            self.instance.validate_unique(exclude=exclude)
        except ValidationError as exc:
            self._update_errors(exc)

    def clean_username(self):
        """Usernames may not contain '@', so one can never pass for an email (SEC-02)."""
        username = self.cleaned_data.get('username') or ''
        if '@' in username:
            raise forms.ValidationError("Usernames cannot contain '@'.")
        return username

    def clean_email(self):
        """
        Reject a duplicate regardless of case.

        This checked `filter(email=email)` — an exact match — while the login
        backend resolves with `__iexact`. So 'A@x.com' could be registered after
        'a@x.com' existed, and which account a later email login reached was
        decided by row order.
        """
        email = (self.cleaned_data['email'] or '').strip().lower()
        # With verification on, a taken address is not revealed here: the view
        # answers identically and mails the owner (audit SEC-14).
        from .auth_service import verification_required
        if not verification_required() and CustomUser.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email


class LoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Email or Username'


class ProfileForm(forms.ModelForm):

    def clean_profile_picture(self):
        """
        Validate by magic bytes, not by the browser-declared content type.

        Django's ImageField validation only runs through a form, and even then
        Pillow accepts formats we do not want to serve back — notably SVG-like
        payloads slipped past a permissive check. Reuse the same allowlist the
        API uses so both surfaces agree.
        """
        pic = self.cleaned_data.get('profile_picture')
        if not pic or not hasattr(pic, 'read'):
            return pic
        from apps.medical_records.services import validate_image_upload
        ok, message = validate_image_upload(pic)
        if not ok:
            raise forms.ValidationError(message)
        return pic

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number', '')
        validate_phone(phone)
        return phone

    class Meta:
        model   = CustomUser
        # No 'email': changing it needs the current password and a link sent to
        # the new address (security_views.email_change, audit SEC-05). Editing
        # it here let a borrowed session redirect password resets.
        fields  = ["first_name", "last_name", "phone_number", "date_of_birth", "profile_picture"]
        widgets = {
            "first_name":      forms.TextInput(attrs=_FC),
            "last_name":       forms.TextInput(attrs=_FC),
            "phone_number":    forms.TextInput(attrs={**_FC, 'type': 'tel'}),
            "date_of_birth":   forms.DateInput(attrs={**_FC, 'type': 'date'}),
            "profile_picture": forms.ClearableFileInput(attrs=_FC),
        }


class PatientProfileForm(forms.ModelForm):
    class Meta:
        model   = PatientProfile
        fields  = ['blood_type', 'allergies', 'emergency_contact_name', 'emergency_contact_phone']
        widgets = {
            'blood_type':               forms.Select(attrs=_FC),
            'allergies':                forms.Textarea(attrs={**_FC, 'rows': 2,
                                            'placeholder': 'List any known allergies, one per line…'}),
            'emergency_contact_name':   forms.TextInput(attrs=_FC),
            'emergency_contact_phone':  forms.TextInput(attrs={**_FC, 'type': 'tel'}),
        }

    def clean_emergency_contact_phone(self):
        phone = self.cleaned_data.get('emergency_contact_phone', '')
        validate_phone(phone)
        return phone


class DoctorProfileForm(forms.ModelForm):
    class Meta:
        model   = DoctorProfile
        fields  = ['specialty', 'license_number', 'hospital', 'department']
        widgets = {
            'specialty':      forms.TextInput(attrs=_FC),
            'license_number': forms.TextInput(attrs=_FC),
            'hospital':       forms.TextInput(attrs=_FC),
            'department':     forms.TextInput(attrs=_FC),
        }


class DataScientistProfileForm(forms.ModelForm):
    class Meta:
        model   = DataScientistProfile
        fields  = ['job_title', 'institution', 'research_area']
        widgets = {
            'job_title':     forms.Select(attrs=_FC),
            'institution':   forms.TextInput(attrs=_FC),
            'research_area': forms.TextInput(attrs=_FC),
        }


class PasswordChangeForm(DjangoPasswordChangeForm):
    pass



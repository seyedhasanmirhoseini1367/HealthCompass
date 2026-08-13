import re

from django import forms
from django.contrib.auth.forms import (UserCreationForm, AuthenticationForm,
                                        PasswordChangeForm as DjangoPasswordChangeForm)
from .models import CustomUser, PatientProfile, DoctorProfile, DataScientistProfile

_FC = {'class': 'form-control'}
_PHONE_RE = re.compile(r'^[\d\s\+\-\(\)\.]{0,20}$')


class RegisterForm(UserCreationForm):
    email      = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=50, required=False)
    last_name  = forms.CharField(max_length=50, required=False)

    class Meta:
        model  = CustomUser
        fields = ["username", "first_name", "last_name", "email", "password1", "password2"]

    def clean_email(self):
        email = self.cleaned_data['email']
        if CustomUser.objects.filter(email=email).exists():
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

    class Meta:
        model   = CustomUser
        fields  = ["first_name", "last_name", "email", "phone_number", "date_of_birth", "profile_picture"]
        widgets = {
            "first_name":      forms.TextInput(attrs=_FC),
            "last_name":       forms.TextInput(attrs=_FC),
            "email":           forms.EmailInput(attrs=_FC),
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
        if phone and not _PHONE_RE.match(phone):
            raise forms.ValidationError('Enter a valid phone number (digits, spaces, +, -, ( ) allowed).')
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
        fields  = ['institution', 'research_area']
        widgets = {
            'institution':   forms.TextInput(attrs=_FC),
            'research_area': forms.TextInput(attrs=_FC),
        }


class PasswordChangeForm(DjangoPasswordChangeForm):
    pass



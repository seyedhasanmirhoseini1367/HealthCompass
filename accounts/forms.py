from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm, PasswordChangeForm as DjangoPasswordChangeForm
from .models import CustomUser


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)
    role = forms.ChoiceField(choices=[
        (CustomUser.Role.PATIENT, "Patient"),
        (CustomUser.Role.DOCTOR, "Doctor / Nurse"),
        (CustomUser.Role.DATA_SCIENTIST, "Data Scientist / Researcher"),
        (CustomUser.Role.HOSPITAL_ADMIN, "Hospital Admin"),
    ])
    first_name = forms.CharField(max_length=50, required=False)
    last_name = forms.CharField(max_length=50, required=False)

    class Meta:
        model = CustomUser
        fields = ["username", "first_name", "last_name", "email", "role", "password1", "password2"]

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
    class Meta:
        model = CustomUser
        fields = ["first_name", "last_name", "email", "phone_number", "date_of_birth", "profile_picture"]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
        }


class PasswordChangeForm(DjangoPasswordChangeForm):
    pass

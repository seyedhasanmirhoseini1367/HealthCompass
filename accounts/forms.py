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


class LoginForm(AuthenticationForm):
    pass


class ProfileForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields = ["first_name", "last_name", "email", "phone_number", "date_of_birth", "profile_picture"]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
        }


class PasswordChangeForm(DjangoPasswordChangeForm):
    pass

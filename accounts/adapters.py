from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.exceptions import ImmediateHttpResponse
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect


class HealthCompassSocialAdapter(DefaultSocialAccountAdapter):

    def pre_social_login(self, request, sociallogin):
        """
        1. If the social account already exists: block unapproved users.
        2. If it's a NEW social login but the email matches an existing account:
           auto-connect and log in directly — skip the role-selection form.
           Existing users already chose their role at registration.
        """
        if sociallogin.is_existing:
            user = sociallogin.user
            if not user.is_approved:
                messages.warning(request, "Your account is pending admin approval.")
                raise ImmediateHttpResponse(redirect('accounts:login'))
            return  # approved existing social account — continue normally

        # Social account not yet connected — check if email matches existing user
        User = get_user_model()
        email = None
        if sociallogin.email_addresses:
            email = sociallogin.email_addresses[0].email
        if not email:
            return  # no email from Google — let allauth handle it normally

        try:
            existing_user = User.objects.get(email=email)
        except User.DoesNotExist:
            return  # genuinely new user — show role-selection form

        # Existing user logging in with Google for the first time:
        # connect the social account to their existing account and log them in.
        if not existing_user.is_approved:
            messages.warning(request, "Your account is pending admin approval.")
            raise ImmediateHttpResponse(redirect('accounts:login'))

        sociallogin.connect(request, existing_user)
        raise ImmediateHttpResponse(redirect('dashboard:home'))

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        from accounts.models import PatientProfile
        PatientProfile.objects.get_or_create(user=user)
        return user

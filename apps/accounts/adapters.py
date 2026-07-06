from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.exceptions import ImmediateHttpResponse
from django.contrib import messages
from django.shortcuts import redirect


class HealthCompassSocialAdapter(DefaultSocialAccountAdapter):

    def pre_social_login(self, request, sociallogin):
        """Block unapproved existing users from logging in via Google."""
        if not sociallogin.is_existing:
            return  # new social account — let allauth handle email-match linking
        user = sociallogin.user
        if not user.is_approved:
            messages.warning(request, "Your account is pending admin approval.")
            raise ImmediateHttpResponse(redirect('/accounts/login/'))

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        from apps.accounts.models import PatientProfile
        PatientProfile.objects.get_or_create(user=user)
        return user

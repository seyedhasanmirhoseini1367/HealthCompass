from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.exceptions import ImmediateHttpResponse
from django.contrib import messages
from django.shortcuts import redirect


class HealthCompassSocialAdapter(DefaultSocialAccountAdapter):

    def pre_social_login(self, request, sociallogin):
        """Block unapproved users from logging in via Google."""
        if sociallogin.is_existing:
            user = sociallogin.user
            if not user.is_approved:
                messages.warning(request, "Your account is pending admin approval.")
                raise ImmediateHttpResponse(redirect('accounts:login'))

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        from accounts.views import ROLES_REQUIRING_APPROVAL, _notify_admin_new_registration
        if user.role in ROLES_REQUIRING_APPROVAL:
            _notify_admin_new_registration(user)
        return user

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
try:
    from allauth.core.exceptions import ImmediateHttpResponse  # allauth >= 65.x
except ImportError:
    from allauth.exceptions import ImmediateHttpResponse        # allauth < 65.x
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


class HealthCompassAccountAdapter(DefaultAccountAdapter):
    """The web login runs through allauth's perform_login (for the MFA stage);
    the app shows its own welcome message, so allauth's is dropped."""

    _SUPPRESSED = {'account/messages/logged_in.txt', 'account/messages/logged_out.txt'}

    def add_message(self, request, level, message_template=None, message_context=None,
                    extra_tags='', message=None):
        if message_template in self._SUPPRESSED:
            return
        return super().add_message(request, level, message_template=message_template,
                                   message_context=message_context, extra_tags=extra_tags,
                                   message=message)

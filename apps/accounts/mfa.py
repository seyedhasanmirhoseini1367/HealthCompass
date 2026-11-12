"""
Two-factor authentication is required for accounts that can read other
people's health data: staff, clinicians, Staff (data scientists) and hospital
admins (audit SEC-17). Optional for patients.

allauth.mfa provides TOTP and recovery codes. This middleware only decides who
must have them: a privileged account with no authenticator is sent to the TOTP
enrolment page before any other page.

Signing in is covered too: the web login goes through allauth's perform_login,
whose login stages ask for the code when the account has an authenticator.
"""
from django.conf import settings
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse

PRIVILEGED_ROLES = {'doctor', 'data_scientist', 'hospital_admin'}

# Reachable without MFA: allauth (enrolment, MFA, logout), our logout, the API
# (token auth, not sessions), static and media, health probes.
_EXEMPT_PREFIXES = ('/auth/', '/accounts/logout/', '/api/', '/static/', '/media/',
                    '/health/', '/favicon.ico')


def mfa_required_for(user) -> bool:
    if not (user and user.is_authenticated):
        return False
    return bool(user.is_staff or user.is_superuser
                or getattr(user, 'role', '') in PRIVILEGED_ROLES)


def has_mfa(user) -> bool:
    from allauth.mfa.models import Authenticator

    return Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()


class RequireMFAMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(settings, 'MFA_ENFORCED', True):
            user = getattr(request, 'user', None)
            if (not request.path.startswith(_EXEMPT_PREFIXES)
                    and mfa_required_for(user) and not has_mfa(user)):
                try:
                    target = reverse('mfa_activate_totp')
                except NoReverseMatch:
                    target = None
                if target and request.path != target:
                    return redirect(f'{target}?next={request.path}')
        return self.get_response(request)

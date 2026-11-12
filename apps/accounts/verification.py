"""
Email verification gate (audit SEC-05).

When ACCOUNT_EMAIL_VERIFICATION is 'mandatory', an account that has not
confirmed its address can sign in and read its own records, but cannot share
data with other people or send questions to the assistant. Both send health
data somewhere, and an unconfirmed address may belong to someone else.

With verification off (no SMTP configured) the gate is open: nobody could ever
confirm, and locking everyone out would be worse than not checking.
"""
from functools import wraps

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

from .auth_service import needs_verification

MESSAGE = ('Confirm your email address first. We sent you a link; you can ask '
           'for a new one from the banner at the top of the page.')


def require_verified_email(view):
    """Refuse the view for unverified accounts: JSON 403 for API and AJAX
    callers, a message and a redirect back for ordinary form posts."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if needs_verification(getattr(request, 'user', None)):
            wants_json = (request.path.startswith('/api/')
                          or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                          or 'application/json' in request.headers.get('Accept', '')
                          or request.content_type == 'application/json')
            if wants_json:
                return JsonResponse({'error': MESSAGE, 'code': 'email_unverified'}, status=403)
            messages.error(request, MESSAGE)
            back = request.META.get('HTTP_REFERER', '')
            if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
                back = 'accounts:profile'
            return redirect(back)
        return view(request, *args, **kwargs)
    return wrapped


def verification_context(request):
    """Context processor: `needs_email_verification` drives the banner;
    `session_seconds` drives the idle warning (static/js/app.js)."""
    from django.conf import settings
    return {'needs_email_verification': needs_verification(getattr(request, 'user', None)),
            'session_seconds': settings.SESSION_COOKIE_AGE,
            'site_url': f'{settings.ACCOUNT_DEFAULT_HTTP_PROTOCOL}://{settings.SITE_DOMAIN}'}

"""
Account-security flows that need proof of mailbox ownership (audit SEC-05,
SEC-13): changing the email address, deleting an account that has no password,
and finishing a password reset.

Links are signed with django.core.signing and carry the account's current
email, so a link stops working once the address it was issued for changes.
"""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetConfirmView
from django.core import signing
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from . import auth_service

logger = logging.getLogger(__name__)

EMAIL_CHANGE_SALT = 'healthcompass.email-change'
EMAIL_CHANGE_MAX_AGE = 24 * 3600
DELETE_SALT = 'healthcompass.account-delete'
DELETE_MAX_AGE = 30 * 60


def _absolute(request, path: str) -> str:
    if request is not None:
        return request.build_absolute_uri(path)
    scheme = 'http' if settings.DEBUG else 'https'
    return f'{scheme}://{settings.SITE_DOMAIN}{path}'


def _mail(to: str, subject: str, body: str) -> bool:
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to])
        return True
    except Exception as exc:
        logger.warning('Account mail "%s" failed: %s', subject, type(exc).__name__)
        return False


# ── Email change ─────────────────────────────────────────────────────────────

def make_email_change_token(user, new_email: str) -> str:
    return signing.dumps({'u': user.pk, 'old': user.email.lower(), 'new': new_email.lower()},
                         salt=EMAIL_CHANGE_SALT)


def read_email_change_token(token: str):
    """(user, new_email) for a valid, unexpired token whose account still has
    the address it was issued against; None otherwise."""
    try:
        data = signing.loads(token, salt=EMAIL_CHANGE_SALT, max_age=EMAIL_CHANGE_MAX_AGE)
    except signing.BadSignature:
        return None
    user = get_user_model().objects.filter(pk=data.get('u')).first()
    if user is None or (user.email or '').lower() != data.get('old'):
        return None
    return user, data.get('new')


@login_required
@ratelimit(key='user', rate='5/h', method='POST', block=False)
def email_change(request):
    """Ask for a new address. Requires the current password (or, for accounts
    without one, nothing more than the session) and mails a link to the NEW
    address; the address changes only when that link is opened."""
    user = request.user
    if request.method == 'POST':
        if getattr(request, 'limited', False):
            messages.error(request, 'Too many attempts. Please try again later.')
            return redirect('accounts:email_change')
        new_email = (request.POST.get('new_email') or '').strip().lower()
        password = request.POST.get('password') or ''
        User = get_user_model()
        if user.has_usable_password() and not user.check_password(password):
            messages.error(request, 'Your current password is incorrect.')
        elif '@' not in new_email or len(new_email) > 254:
            messages.error(request, 'Enter a valid email address.')
        elif new_email == (user.email or '').lower():
            messages.error(request, 'That is already your email address.')
        else:
            # Same response whether or not the address is taken, so this form
            # does not reveal which addresses are registered.
            if not User.objects.filter(email__iexact=new_email).exists():
                link = _absolute(request, reverse('accounts:email_change_confirm',
                                                  args=[make_email_change_token(user, new_email)]))
                _mail(new_email, 'Confirm your new HealthCompass email address',
                      f'Open this link to make {new_email} the email address of your '
                      f'HealthCompass account:\n\n{link}\n\nThe link works for 24 hours. '
                      'If you did not ask for this, ignore this message.')
            messages.success(request, f'We sent a confirmation link to {new_email}. '
                                      'Your address changes when you open it.')
            return redirect('accounts:profile')
    return render(request, 'accounts/email_change.html', {'has_password': user.has_usable_password()})


def email_change_confirm(request, token):
    """GET shows a confirm button; POST swaps the address (a GET must not
    change account state, since mail scanners prefetch links)."""
    result = read_email_change_token(token)
    if result is None:
        messages.error(request, 'This link is invalid or has expired.')
        return redirect('accounts:profile' if request.user.is_authenticated else 'accounts:login')
    user, new_email = result
    if request.method != 'POST':
        return render(request, 'accounts/email_change_confirm.html', {'new_email': new_email})

    User = get_user_model()
    if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
        messages.error(request, 'That address is now used by another account.')
        return redirect('accounts:profile' if request.user.is_authenticated else 'accounts:login')

    from allauth.account.models import EmailAddress

    old_email = user.email
    user.email = new_email
    user.save(update_fields=['email'])
    EmailAddress.objects.filter(user=user).delete()
    auth_service.record_email_address(user, verified=True)  # the link proved ownership
    auth_service.revoke_all_tokens(user)
    if old_email:
        _mail(old_email, 'Your HealthCompass email address was changed',
              f'The email address of your HealthCompass account was changed to {new_email}. '
              'If you did not do this, reset your password and contact us.')
    messages.success(request, 'Your email address has been updated.')
    return redirect('accounts:profile' if request.user.is_authenticated else 'accounts:login')


# ── Account deletion without a password ──────────────────────────────────────

def make_delete_token(user) -> str:
    return signing.dumps({'u': user.pk, 'e': (user.email or '').lower()}, salt=DELETE_SALT)


def read_delete_token(token: str):
    try:
        data = signing.loads(token, salt=DELETE_SALT, max_age=DELETE_MAX_AGE)
    except signing.BadSignature:
        return None
    user = get_user_model().objects.filter(pk=data.get('u')).first()
    if user is None or (user.email or '').lower() != data.get('e'):
        return None
    return user


def send_delete_link(request, user) -> bool:
    link = _absolute(request, reverse('accounts:delete_account_confirm', args=[make_delete_token(user)]))
    return _mail(user.email, 'Confirm deleting your HealthCompass account',
                 'Open this link to permanently delete your HealthCompass account and all '
                 f'of its data:\n\n{link}\n\nThe link works for 30 minutes. If you did not ask '
                 'for this, ignore this message; nothing has been deleted.')


def delete_account_confirm(request, token):
    """The emailed link. GET shows a final confirmation; POST deletes."""
    user = read_delete_token(token)
    if user is None:
        messages.error(request, 'This link is invalid or has expired.')
        return redirect('home')
    if request.method != 'POST':
        return render(request, 'accounts/delete_account_confirm.html', {'email': user.email})
    if request.user.is_authenticated:
        logout(request)
    from .services import purge_user_data
    auth_service.revoke_all_tokens(user)
    purge_user_data(user)
    messages.success(request, 'Your account and all associated data have been permanently deleted.')
    return redirect('home')


# ── Password reset completion ────────────────────────────────────────────────

class HealthCompassPasswordResetConfirmView(PasswordResetConfirmView):
    """Completing a reset proves mailbox ownership (marks the address verified,
    which also makes linking Google possible) and revokes every API token."""
    template_name = 'accounts/password_reset_confirm.html'
    success_url = '/accounts/password/reset/complete/'

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        auth_service.mark_email_verified(user)
        auth_service.revoke_all_tokens(user)
        return response


@login_required
@require_POST
def resend_verification(request):
    auth_service.send_verification(request, request.user)
    messages.success(request, f'We sent a confirmation link to {request.user.email}.')
    nxt = request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = 'accounts:profile'
    return redirect(nxt)


@login_required
@require_POST
def session_ping(request):
    """Keep-alive for the idle warning dialog: touching the session moves its
    expiry SESSION_COOKIE_AGE forward (IdleSessionMiddleware)."""
    import time

    from django.http import JsonResponse

    from .idle_session import KEY
    request.session[KEY] = int(time.time())
    return JsonResponse({'ok': True, 'seconds': settings.SESSION_COOKIE_AGE})

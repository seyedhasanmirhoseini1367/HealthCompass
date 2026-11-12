"""
Account rules shared by the web views and the API (audit SEC-08, CQ-02).

The two surfaces each used to implement registration, login and password change
on their own, and drifted: the API accepted 'password' as a password, admitted
accounts pending approval and never revoked tokens. Every rule now lives here
once and both layers call it.
"""
import logging

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .usernames import generate_username

logger = logging.getLogger(__name__)

# Wording shared by both surfaces, so a client cannot tell them apart.
MSG_INVALID = 'Invalid email or password.'
MSG_PENDING = 'Your account is pending admin approval.'
MSG_INACTIVE = 'This account is inactive.'
MSG_EMAIL_TAKEN = 'An account with this email already exists.'


class AuthError(Exception):
    """A refusal with a user-facing message and a short machine code."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


# ── Passwords ────────────────────────────────────────────────────────────────

def password_errors(password: str, user=None) -> list[str]:
    """AUTH_PASSWORD_VALIDATORS messages for this password, empty when it passes."""
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        return list(exc.messages)
    return []


def change_password(user, old: str, new: str) -> None:
    """Check the old password, validate the new one, save it and revoke every
    issued API token. Raises AuthError or ValidationError."""
    if not user.check_password(old or ''):
        raise AuthError('Current password is incorrect.', 'wrong_password')
    validate_password(new, user=user)
    user.set_password(new)
    user.save(update_fields=['password'])
    revoke_all_tokens(user)


# ── Login ────────────────────────────────────────────────────────────────────

def check_login_allowed(user) -> None:
    """Raise AuthError when an authenticated account may not sign in yet."""
    if not user.is_active:
        raise AuthError(MSG_INACTIVE, 'inactive')
    if not getattr(user, 'is_approved', True):
        raise AuthError(MSG_PENDING, 'pending_approval')


def authenticate_identifier(request, identifier: str, password: str):
    """Authenticate by email (or legacy username). Returns the user or raises.

    EmailOrUsernameBackend runs the password hasher on the not-found branch,
    so the response time does not reveal whether an account exists.
    """
    user = authenticate(request, username=(identifier or '').strip(), password=password or '')
    if user is None:
        raise AuthError(MSG_INVALID, 'invalid_credentials')
    check_login_allowed(user)
    return user


# ── Registration ─────────────────────────────────────────────────────────────

def register_patient(email: str, password: str, first_name: str = '', last_name: str = '',
                     *, username: str | None = None, request=None):
    """Create a patient account, its profile and its (unverified) EmailAddress,
    and send the confirmation mail. Raises AuthError or ValidationError."""
    from .models import PatientProfile

    User = get_user_model()
    email = (email or '').strip().lower()
    if User.objects.filter(email__iexact=email).exists():
        raise AuthError(MSG_EMAIL_TAKEN, 'email_taken')
    validate_password(password, user=User(email=email, first_name=first_name, last_name=last_name))

    user = None
    for _ in range(5 if username is None else 1):
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username or generate_username(email), email=email,
                    password=password, first_name=first_name or '', last_name=last_name or '',
                    role=User.Role.PATIENT)
                PatientProfile.objects.get_or_create(user=user)
            break
        except IntegrityError:
            if User.objects.filter(email__iexact=email).exists():
                raise AuthError(MSG_EMAIL_TAKEN, 'email_taken') from None
            if username is not None:
                raise AuthError('That username is taken.', 'username_taken') from None
    if user is None:
        raise AuthError('Could not create the account. Try again.', 'retry')

    record_email_address(user, verified=False)
    send_verification(request, user)
    return user


REGISTER_DONE_MESSAGE = ('We sent a message to {email}. Open the link in it to confirm your '
                         'address. If you already have an account, the message explains how '
                         'to sign in instead.')


def notify_existing_registration(request, email: str) -> None:
    """Someone tried to register an address that already has an account. Tell
    the owner by mail instead of telling the visitor (audit SEC-14)."""
    from django.core.mail import send_mail

    base = (request.build_absolute_uri('/') if request is not None
            else f'https://{settings.SITE_DOMAIN}/')
    try:
        send_mail(
            'Someone tried to create a HealthCompass account with your address',
            'Somebody tried to sign up for HealthCompass with this email address, which '
            'already has an account. If it was you, sign in instead, or reset your '
            f'password at {base}accounts/password/reset/ . If it was not you, you can '
            'ignore this message; nothing about your account has changed.',
            settings.DEFAULT_FROM_EMAIL, [email])
    except Exception as exc:
        logger.warning('Existing-account notice failed: %s', type(exc).__name__)


def register_or_notify(email, password, first_name='', last_name='', *, username=None,
                       request=None):
    """With verification on, registration answers the same way whether or not
    the address is taken: a new address gets an account and a confirmation
    mail, a taken one gets a notice to its owner. Returns the new user or None."""
    User = get_user_model()
    if User.objects.filter(email__iexact=(email or '').strip()).exists():
        # Hash a password anyway so timing matches the new-account path.
        User().set_password(password)
        notify_existing_registration(request, (email or '').strip().lower())
        return None
    return register_patient(email, password, first_name, last_name,
                            username=username, request=request)


# ── Email ownership ──────────────────────────────────────────────────────────

def verification_required() -> bool:
    return getattr(settings, 'ACCOUNT_EMAIL_VERIFICATION', 'none') == 'mandatory'


def record_email_address(user, *, verified: bool):
    """Ensure the allauth EmailAddress row for the user's current email exists
    (primary). Only ever raises `verified`, never lowers it."""
    from allauth.account.models import EmailAddress

    if not user.email:
        return None
    address, _ = EmailAddress.objects.get_or_create(
        user=user, email__iexact=user.email,
        defaults={'email': user.email, 'primary': True, 'verified': verified})
    changed = []
    if verified and not address.verified:
        address.verified = True
        changed.append('verified')
    if not address.primary:
        EmailAddress.objects.filter(user=user).exclude(pk=address.pk).update(primary=False)
        address.primary = True
        changed.append('primary')
    if changed:
        address.save(update_fields=changed)
    return address


def email_verified(user) -> bool:
    from allauth.account.models import EmailAddress

    return bool(user.email) and EmailAddress.objects.filter(
        user=user, email__iexact=user.email, verified=True).exists()


def needs_verification(user) -> bool:
    """True when verification is enforced and this user has not proven the address."""
    return bool(user and user.is_authenticated and verification_required()
                and not email_verified(user))


def send_verification(request, user) -> None:
    """Send allauth's confirmation mail for the user's primary address.

    Only when verification is configured: without SMTP the mail would go to the
    console backend and nobody could act on it.
    """
    if not verification_required():
        return
    address = record_email_address(user, verified=False)
    if address is None or address.verified:
        return
    try:
        address.send_confirmation(request, signup=True)
    except Exception as exc:  # mail failure must not break sign-up
        logger.warning('Verification mail for user %s failed: %s', user.pk, type(exc).__name__)


def mark_email_verified(user) -> None:
    """A completed password reset proves mailbox ownership."""
    record_email_address(user, verified=True)


# ── API tokens ───────────────────────────────────────────────────────────────

def revoke_all_tokens(user) -> int:
    """Blacklist every outstanding refresh token for this user. Returns how many."""
    try:
        from rest_framework_simplejwt.token_blacklist.models import (BlacklistedToken,
                                                                     OutstandingToken)
    except (ImportError, RuntimeError):
        return 0
    count = 0
    for token in OutstandingToken.objects.filter(user=user):
        _, created = BlacklistedToken.objects.get_or_create(token=token)
        count += int(created)
    return count

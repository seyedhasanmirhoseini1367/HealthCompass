"""
Deployment system checks for the accounts app.

These exist because two of this project's settings can be individually
reasonable and jointly broken, in ways that are invisible until a user is
already locked out or already compromised.
"""
from django.conf import settings
from django.core.checks import Error, Warning, register

_CONSOLE_BACKEND = 'django.core.mail.backends.console.EmailBackend'


@register('accounts')
def check_email_verification_is_deliverable(app_configs, **kwargs):
    """
    Mandatory email verification with no way to send email locks everyone out.

    settings.py selects the SMTP backend only when EMAIL_HOST_USER and
    EMAIL_HOST_PASSWORD are both set, and silently falls back to the console
    backend otherwise. If ACCOUNT_EMAIL_VERIFICATION is ever set to 'mandatory'
    while that fallback is active, every confirmation mail is printed to stdout
    and no user can ever complete registration.

    This is an Error rather than a Warning: the failure is total, and it appears
    only after deployment when real users try to sign up.
    """
    verification = getattr(settings, 'ACCOUNT_EMAIL_VERIFICATION', 'none')
    backend      = getattr(settings, 'EMAIL_BACKEND', '')

    if verification == 'mandatory' and backend == _CONSOLE_BACKEND:
        return [Error(
            'ACCOUNT_EMAIL_VERIFICATION is "mandatory" but EMAIL_BACKEND is the '
            'console backend, so confirmation emails are never delivered and no '
            'user can complete registration.',
            hint='Set EMAIL_HOST_USER and EMAIL_HOST_PASSWORD so settings.py '
                 'selects the SMTP backend, or set ACCOUNT_EMAIL_VERIFICATION '
                 'to "optional"/"none".',
            id='accounts.E001',
        )]
    return []


@register('accounts')
def check_social_auto_connect_is_guarded(app_configs, **kwargs):
    """
    Auto-connecting a social identity to an unverified local account is an
    account pre-hijack.

    With SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT enabled and email
    verification off, an attacker can register the victim's address, wait for
    the victim to sign in with Google, and receive their identity on the
    attacker-controlled account.

    apps/accounts/views.py AutoCompleteSocialSignup guards this by refusing to
    connect unless the local account's address is verified. This check exists so
    that the combination is at least visible to whoever runs `manage.py check`,
    and so removing the guard while these settings stand does not go unnoticed.
    """
    auto_connect = getattr(settings, 'SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT', False)
    verification = getattr(settings, 'ACCOUNT_EMAIL_VERIFICATION', 'none')

    if auto_connect and verification == 'none':
        return [Warning(
            'SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT is enabled while '
            'ACCOUNT_EMAIL_VERIFICATION is "none". Social identities may only be '
            'connected to accounts that have verified the address; this is '
            'currently enforced in code by AutoCompleteSocialSignup.',
            hint='Keep the verification guard in AutoCompleteSocialSignup.get(), '
                 'or set ACCOUNT_EMAIL_VERIFICATION to "mandatory" once SMTP is '
                 'configured (see accounts.E001).',
            id='accounts.W001',
        )]
    return []

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


@register('accounts')
def check_email_verification_is_enabled(app_configs, **kwargs):
    """
    Without SMTP, ACCOUNT_EMAIL_VERIFICATION falls back to 'none': anyone can
    register someone else's address, and nothing proves who owns an account's
    email (audit SEC-05). Sharing and the assistant are gated on verification
    only while it is enabled.
    """
    if getattr(settings, 'ACCOUNT_EMAIL_VERIFICATION', 'none') != 'mandatory':
        return [Warning(
            'Email verification is off: no account has to prove it owns its address.',
            hint='Configure SMTP (EMAIL_HOST_USER and EMAIL_HOST_PASSWORD); verification '
                 'then defaults to "mandatory".',
            id='accounts.W002',
        )]
    return []


@register('accounts', deploy=False)
def check_shared_cache_in_production(app_configs, **kwargs):
    """
    Rate limits, throttles and the per-account login counter live in the cache.
    With the default LocMem backend each worker process counts on its own, so a
    limit of 10/minute is really 10/minute per worker, and a restart forgets
    every lockout (audit SEC-06). Production needs a shared cache (Redis).
    """
    if settings.DEBUG or getattr(settings, 'ALLOW_LOCAL_CACHE', False):
        return []
    backend = settings.CACHES.get('default', {}).get('BACKEND', '')
    if backend.endswith('LocMemCache'):
        return [Error(
            'DEBUG is False but the cache is per-process memory, so rate limits are '
            'counted per worker and lockouts do not hold.',
            hint='Set CACHE_URL to a Redis instance, or ALLOW_LOCAL_CACHE=True to accept this.',
            id='accounts.E002',
        )]
    return []



def _registrable(domain: str) -> str:
    parts = [p for p in (domain or '').lower().split(':')[0].split('.') if p]
    return '.'.join(parts[-2:])


@register('accounts', deploy=True)
def check_sender_matches_site(app_configs, **kwargs):
    """Mail sent from a domain that is not yours fails SPF/DKIM alignment and
    lands in spam, including password resets."""
    sender = getattr(settings, 'DEFAULT_FROM_EMAIL', '') or ''
    domain = sender.rsplit('@', 1)[-1].strip('> ') if '@' in sender else ''
    site = _registrable(getattr(settings, 'SITE_DOMAIN', ''))
    if not domain or domain == 'localhost' or _registrable(domain) != site:
        return [Warning(f'DEFAULT_FROM_EMAIL ({sender or "unset"}) is not an address on {site}.',
                        hint='Send from a domain you control, through a transactional provider.',
                        id='accounts.W003')]
    return []

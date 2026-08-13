"""
REGRESSION — NEW-03: account pre-hijack via unverified email + social auto-connect.

The attack these tests close
----------------------------
1. Attacker registers locally as victim@gmail.com with a password of their
   choosing. ACCOUNT_EMAIL_VERIFICATION is 'none', so no confirmation mail is
   ever sent and the victim never learns the account exists.
2. Victim later signs in with Google. allauth matches the address, and
   `AutoCompleteSocialSignup.get()` called `sociallogin.connect(request,
   existing_user)` for ANY match — handing the victim's Google identity to the
   attacker's account.
3. The victim then uploads medical records into an account the attacker can
   still log into with the original password.

Why the fix is the connect guard and not mandatory verification
----------------------------------------------------------------
Setting ACCOUNT_EMAIL_VERIFICATION = 'mandatory' would also close the hole, but
settings.py selects the console email backend whenever EMAIL_HOST_USER /
EMAIL_HOST_PASSWORD are unset — so on a deployment without SMTP it would lock
every user out of registration instead. The guard closes the vulnerability
without depending on mail delivery. `accounts.E001` covers the other half by
failing `manage.py check` if verification is ever made mandatory while mail is
undeliverable.

Refusing to connect degrades to a denial of service against a squatted address —
recoverable by password reset, which itself proves ownership — rather than a
silent takeover. That trade is the point.
"""
from django.contrib.auth import get_user_model
from django.core.checks import Error, Warning
from django.test import TestCase, override_settings

from apps.accounts.checks import (
    check_email_verification_is_deliverable, check_social_auto_connect_is_guarded,
)
from apps.accounts.views import _email_is_verified

CONSOLE = 'django.core.mail.backends.console.EmailBackend'
SMTP    = 'django.core.mail.backends.smtp.EmailBackend'


class EmailVerificationHelperTests(TestCase):
    """The predicate the guard depends on. It must fail closed."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='victim', password='pw-test-only', email='victim@example.com')

    def test_account_with_no_email_address_row_is_unverified(self):
        """A local registration proves nothing about mailbox ownership."""
        self.assertFalse(_email_is_verified(self.user, 'victim@example.com'))

    def test_unconfirmed_email_address_row_is_unverified(self):
        from allauth.account.models import EmailAddress
        EmailAddress.objects.create(
            user=self.user, email='victim@example.com', verified=False, primary=True)
        self.assertFalse(_email_is_verified(self.user, 'victim@example.com'))

    def test_confirmed_email_address_row_is_verified(self):
        from allauth.account.models import EmailAddress
        EmailAddress.objects.create(
            user=self.user, email='victim@example.com', verified=True, primary=True)
        self.assertTrue(_email_is_verified(self.user, 'victim@example.com'))

    def test_matching_is_case_insensitive(self):
        from allauth.account.models import EmailAddress
        EmailAddress.objects.create(
            user=self.user, email='Victim@Example.com', verified=True, primary=True)
        self.assertTrue(_email_is_verified(self.user, 'victim@example.com'))

    def test_verification_belonging_to_another_user_does_not_count(self):
        """The row must belong to THIS account, or one user's proof frees another."""
        from allauth.account.models import EmailAddress
        other = get_user_model().objects.create_user(
            username='other', password='pw-test-only', email='other@example.com')
        EmailAddress.objects.create(
            user=other, email='victim@example.com', verified=True, primary=True)
        self.assertFalse(_email_is_verified(self.user, 'victim@example.com'))

    def test_helper_fails_closed_when_allauth_cannot_be_read(self):
        """An error must never be read as 'verified'."""
        from unittest.mock import patch
        with patch('allauth.account.models.EmailAddress.objects.filter',
                   side_effect=RuntimeError('db down')):
            self.assertFalse(_email_is_verified(self.user, 'victim@example.com'))


class GuardIsPresentTests(TestCase):
    """
    Structural: the connect call must remain gated.

    A behavioural test would need to drive allauth's full social pipeline; this
    asserts the invariant that matters — connect() is not reachable without the
    verification check.
    """

    def test_connect_is_guarded_by_a_verification_check(self):
        import ast
        import inspect

        from apps.accounts.views import AutoCompleteSocialSignup

        source = inspect.getsource(AutoCompleteSocialSignup.get)
        tree = ast.parse(source.lstrip())

        connect_calls, verify_calls = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == 'connect':
                    connect_calls.append(node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == '_email_is_verified':
                    verify_calls.append(node.lineno)

        self.assertEqual(len(connect_calls), 1, 'exactly one connect() call expected')
        self.assertTrue(verify_calls, 'connect() must be gated by _email_is_verified')
        self.assertLess(min(verify_calls), connect_calls[0],
                        'the verification check must precede connect()')


class DeploymentCheckTests(TestCase):
    """accounts.E001 / W001 — settings combinations that are jointly broken."""

    @override_settings(ACCOUNT_EMAIL_VERIFICATION='mandatory', EMAIL_BACKEND=CONSOLE)
    def test_mandatory_verification_without_smtp_is_an_error(self):
        """
        The lockout footgun: every confirmation mail goes to stdout, so nobody
        can ever finish registering.
        """
        results = check_email_verification_is_deliverable(None)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Error)
        self.assertEqual(results[0].id, 'accounts.E001')

    @override_settings(ACCOUNT_EMAIL_VERIFICATION='mandatory', EMAIL_BACKEND=SMTP)
    def test_mandatory_verification_with_smtp_is_fine(self):
        self.assertEqual(check_email_verification_is_deliverable(None), [])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION='none', EMAIL_BACKEND=CONSOLE)
    def test_console_backend_alone_is_fine(self):
        """Console mail is normal in development; only the combination is fatal."""
        self.assertEqual(check_email_verification_is_deliverable(None), [])

    @override_settings(SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT=True,
                       ACCOUNT_EMAIL_VERIFICATION='none')
    def test_auto_connect_without_verification_warns(self):
        results = check_social_auto_connect_is_guarded(None)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Warning)
        self.assertEqual(results[0].id, 'accounts.W001')

    @override_settings(SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT=False,
                       ACCOUNT_EMAIL_VERIFICATION='none')
    def test_no_warning_when_auto_connect_is_disabled(self):
        self.assertEqual(check_social_auto_connect_is_guarded(None), [])

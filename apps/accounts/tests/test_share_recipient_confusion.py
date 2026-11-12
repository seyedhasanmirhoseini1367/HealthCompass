"""Regression tests for audit SEC-02: share recipient confusion.

A username could look like an email. Shares and logins that matched either one
could reach the account whose USERNAME was the address instead of the account
that owns it.
"""
from io import StringIO

from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.forms import RegisterForm
from apps.accounts.models import SharingGrant

User = get_user_model()


class _Squatter(TestCase):
    def setUp(self):
        # A: registered the victim's address as a USERNAME (allowed before the fix).
        self.squatter = User.objects.create_user(
            'victim@x.com', email='a@evil.com', password='pw-squat-1', role='patient')
        # B: the real owner of the address.
        self.owner = User.objects.create_user(
            'realowner', email='victim@x.com', password='pw-owner-1', role='patient')
        self.sharer = User.objects.create_user(
            'sharer', email='sharer@test.invalid', password='pw-share-1', role='patient')


class ShareGoesToEmailOwnerTests(_Squatter):
    def test_web_share_reaches_email_owner(self):
        self.client.force_login(self.sharer)
        self.client.post(reverse('accounts:create_share'),
                         {'recipient': 'victim@x.com', 'scopes': ['records']})
        grant = SharingGrant.objects.get(patient=self.sharer)
        self.assertEqual(grant.recipient, self.owner)

    def test_api_share_reaches_email_owner(self):
        api = APIClient()
        api.force_authenticate(self.sharer)
        response = api.post('/api/v1/sharing/companions/create/', {'identifier': 'victim@x.com', 'scopes': ['records']},
                            format='json')
        self.assertLess(response.status_code, 300, response.content)
        grant = SharingGrant.objects.get(patient=self.sharer)
        self.assertEqual(grant.recipient, self.owner)

    def test_a_bare_username_no_longer_resolves(self):
        self.client.force_login(self.sharer)
        self.client.post(reverse('accounts:create_share'),
                         {'recipient': 'realowner', 'scopes': ['records']})
        self.assertFalse(SharingGrant.objects.exists())


class LoginPrefersEmailTests(_Squatter):
    def test_email_login_reaches_the_owner(self):
        self.assertEqual(authenticate(username='victim@x.com', password='pw-owner-1'), self.owner)

    def test_username_login_still_works_when_no_email_matches(self):
        self.assertEqual(authenticate(username='realowner', password='pw-owner-1'), self.owner)


class UsernameRulesTests(TestCase):
    def test_register_form_rejects_at_in_username(self):
        form = RegisterForm(data={
            'username': 'someone@x.com', 'email': 'someone@x.com',
            'password1': 'Very-strong-pass-42', 'password2': 'Very-strong-pass-42'})
        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)

    def test_api_register_succeeds_when_email_shaped_username_is_taken(self):
        User.objects.create_user('new@x.com', email='other@x.com', password='pw-other-1')
        response = APIClient().post('/api/v1/auth/register/', {
            'email': 'new@x.com', 'password': 'Very-strong-pass-42',
            'password2': 'Very-strong-pass-42'}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        created = User.objects.get(email='new@x.com')
        self.assertNotIn('@', created.username)


class AuditUsernamesCommandTests(_Squatter):
    def test_lists_email_shaped_usernames_that_are_not_their_own_email(self):
        User.objects.create_user('self@x.com', email='self@x.com', password='pw-self-1')
        out = StringIO()
        call_command('audit_usernames', stdout=out)
        text = out.getvalue()
        self.assertIn("'victim@x.com'", text)
        self.assertNotIn("'self@x.com' email", text)
        self.assertIn('1 account(s) flagged', text)

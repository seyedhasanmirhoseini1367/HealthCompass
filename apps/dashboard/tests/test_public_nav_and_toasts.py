"""Logged-out top menu, and flash messages shown as toasts."""
from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class PublicNavTests(TestCase):

    def test_logged_out_menu_offers_the_assistant(self):
        body = self.client.get('/').content.decode()
        self.assertIn('<a href="/assistant/">AI Assistant</a>', body)
        # And in the phone menu.
        self.assertIn('href="/assistant/" class="mm-link"', body)

    def test_assistant_asks_to_log_in_then_comes_back(self):
        r = self.client.get('/assistant/')
        self.assertRedirects(r, '/accounts/login/?next=/assistant/',
                             fetch_redirect_response=False)
        User.objects.create_user('pn_user', email='pn@test.invalid',
                                 password='pw-12345!', role='patient')
        r = self.client.post('/accounts/login/?next=/assistant/',
                             {'username': 'pn_user', 'password': 'pw-12345!'})
        self.assertRedirects(r, '/assistant/', fetch_redirect_response=False)

    def test_google_login_keeps_the_destination(self):
        body = self.client.get('/accounts/login/?next=/assistant/').content.decode()
        self.assertIn('/auth/google/login/?next=/assistant/', body)

    def test_google_login_defaults_to_dashboard(self):
        body = self.client.get('/accounts/login/').content.decode()
        self.assertIn('/auth/google/login/?next=/dashboard/', body)


class ToastTests(TestCase):

    def test_logout_message_is_a_dismissible_toast(self):
        user = User.objects.create_user('tt_user', email='tt@test.invalid',
                                        password='pw', role='patient')
        self.client.force_login(user)
        body = self.client.post('/accounts/logout/', follow=True).content.decode()
        self.assertIn('class="toast-stack"', body)
        self.assertIn('toast toast-success', body)
        self.assertIn('data-autohide="1"', body)
        self.assertIn('aria-label="Dismiss"', body)
        self.assertNotIn('class="messages-wrap"', body)
        self.assertEqual(body.count('You have been logged out'), 1)

    def test_no_toast_stack_without_messages(self):
        self.assertNotIn('toast-stack"', self.client.get('/').content.decode())

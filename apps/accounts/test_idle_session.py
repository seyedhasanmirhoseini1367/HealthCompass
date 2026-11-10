"""Idle sign-out: the session expiry moves with activity, but not on every request."""
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts import idle_session

User = get_user_model()


class IdleSessionTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('is_user', email='is@test.invalid',
                                             password='pw', role='patient')
        self.client.force_login(self.user)

    def test_session_lifetime_is_short(self):
        self.assertLessEqual(settings.SESSION_COOKIE_AGE, 8 * 3600)
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)

    def test_activity_moves_the_expiry_forward(self):
        with mock.patch.object(idle_session.time, 'time', return_value=1_000_000):
            self.client.get('/dashboard/health/')
        first = self.client.session.get_expiry_date()
        with mock.patch.object(idle_session.time, 'time',
                               return_value=1_000_000 + idle_session.TOUCH_SECONDS + 1):
            self.client.get('/dashboard/health/')
        self.assertEqual(self.client.session[idle_session.KEY],
                         1_000_000 + idle_session.TOUCH_SECONDS + 1)
        self.assertGreaterEqual(self.client.session.get_expiry_date(), first)

    def test_rapid_requests_do_not_rewrite_the_session(self):
        with mock.patch.object(idle_session.time, 'time', return_value=2_000_000):
            self.client.get('/dashboard/health/')
        with mock.patch.object(idle_session.time, 'time', return_value=2_000_010):
            self.client.get('/dashboard/health/')
        self.assertEqual(self.client.session[idle_session.KEY], 2_000_000)

    def test_anonymous_requests_are_untouched(self):
        self.client.logout()
        self.client.get('/')
        self.assertNotIn(idle_session.KEY, self.client.session)

"""Prompt 3 of the production-readiness audit: static storage (ARCH-01), client
IP and rate limits (SEC-06, SEC-16), security headers (SEC-10) and the shared
cache check."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from healthcompass.net import client_ip, hash_ip

User = get_user_model()
STRONG = 'Correct-Horse-Battery-77'


def _load_settings_as_if(argv):
    """Execute settings.py as a fresh module with a different sys.argv."""
    path = Path(settings.BASE_DIR) / 'healthcompass' / 'settings.py'
    spec = importlib.util.spec_from_file_location('_settings_probe', path)
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, 'argv', argv):
        spec.loader.exec_module(module)
    return module


class StaticStorageTests(SimpleTestCase):
    def test_production_uses_hashed_compressed_static_files(self):
        probe = _load_settings_as_if(['manage.py', 'runserver'])
        self.assertEqual(probe.STORAGES['staticfiles']['BACKEND'],
                         'whitenoise.storage.CompressedManifestStaticFilesStorage')
        self.assertFalse(hasattr(probe, 'STATICFILES_STORAGE'))

    def test_the_test_runner_uses_the_non_manifest_storage(self):
        self.assertEqual(settings.STORAGES['staticfiles']['BACKEND'],
                         'whitenoise.storage.CompressedStaticFilesStorage')


class ClientIpBehindProxyTests(SimpleTestCase):
    def _req(self, xff=None, remote='10.0.0.9'):
        request = RequestFactory().get('/')
        request.META['REMOTE_ADDR'] = remote
        if xff is not None:
            request.META['HTTP_X_FORWARDED_FOR'] = xff
        return request

    @override_settings(TRUSTED_PROXY_COUNT=0)
    def test_no_proxy_uses_remote_addr(self):
        self.assertEqual(client_ip(self._req('6.6.6.6')), '10.0.0.9')

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_one_proxy_uses_the_right_most_entry(self):
        self.assertEqual(client_ip(self._req('203.0.113.7')), '203.0.113.7')

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_a_spoofed_left_most_value_is_ignored(self):
        for spoof in ('1.1.1.1', '2.2.2.2'):
            self.assertEqual(client_ip(self._req(f'{spoof}, 203.0.113.7')), '203.0.113.7')

    @override_settings(TRUSTED_PROXY_COUNT=2)
    def test_two_proxies_skip_the_inner_hop(self):
        self.assertEqual(client_ip(self._req('9.9.9.9, 203.0.113.7, 10.1.1.1')), '203.0.113.7')

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_missing_header_falls_back_to_remote_addr(self):
        self.assertEqual(client_ip(self._req()), '10.0.0.9')

    def test_ip_hash_is_keyed(self):
        import hashlib
        self.assertNotEqual(hash_ip('1.2.3.4'), hashlib.sha256(b'1.2.3.4').hexdigest())


class PerAccountLoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user('target', email='target@test.invalid', password=STRONG)

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_sixth_failure_is_throttled_from_a_different_ip_on_the_web(self):
        for i in range(5):
            self.client.post(reverse('accounts:login'),
                             {'username': 'target@test.invalid', 'password': 'wrong'},
                             HTTP_X_FORWARDED_FOR=f'198.51.100.{i}')
        response = self.client.post(reverse('accounts:login'),
                                    {'username': 'target@test.invalid', 'password': STRONG},
                                    HTTP_X_FORWARDED_FOR='192.0.2.200')
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_sixth_failure_is_throttled_on_the_api(self):
        api = APIClient()
        for _ in range(5):
            api.post('/api/v1/auth/login/', {'email': 'target@test.invalid', 'password': 'x'},
                     format='json')
        # Reset the per-IP DRF throttles but not the per-account counter.
        from apps.accounts.login_throttle import _key
        kept = cache.get(_key('target@test.invalid'))
        cache.clear()
        cache.set(_key('target@test.invalid'), kept, 900)
        response = api.post('/api/v1/auth/login/',
                            {'email': 'target@test.invalid', 'password': STRONG}, format='json')
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)

    def test_unknown_accounts_are_throttled_the_same_way(self):
        for _ in range(5):
            self.client.post(reverse('accounts:login'),
                             {'username': 'ghost@test.invalid', 'password': 'x'})
        response = self.client.post(reverse('accounts:login'),
                                    {'username': 'ghost@test.invalid', 'password': 'x'})
        self.assertEqual(response.status_code, 429)


class WebUploadRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('uploader', email='up@test.invalid', password=STRONG)
        self.client.force_login(self.user)

    def test_web_uploads_share_the_api_upload_rate(self):
        rates = dict(settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'], upload='2/hour')
        with override_settings(REST_FRAMEWORK=dict(settings.REST_FRAMEWORK, DEFAULT_THROTTLE_RATES=rates)):
            codes = [self.client.post('/records/upload/text/', {'text': 'Hb 140 g/L'}).status_code
                     for _ in range(3)]
        self.assertEqual(codes[-1], 429)

    def test_over_long_pasted_text_is_refused(self):
        with override_settings(MAX_PASTED_TEXT_CHARS=50):
            self.client.post('/records/upload/scan/', {'raw_text': 'x' * 51})
        from apps.medical_records.models import MedicalRecord
        self.assertFalse(MedicalRecord.objects.filter(patient=self.user).exists())


class SecurityHeaderTests(TestCase):
    def test_signed_in_pages_are_no_store_and_noindex(self):
        user = User.objects.create_user('hdr', email='hdr@test.invalid', password=STRONG)
        self.client.force_login(user)
        response = self.client.get('/dashboard/', follow=True)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['X-Robots-Tag'], 'noindex')
        csp = response['Content-Security-Policy']
        self.assertIn("default-src 'self'", csp)
        # Enforcing, and no inline script allowed (prompt 7).
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertEqual(response['Permissions-Policy'], 'camera=(self), microphone=(), geolocation=()')

    def test_the_public_landing_page_is_cacheable(self):
        response = self.client.get('/')
        self.assertNotEqual(response.get('Cache-Control'), 'no-store')
        self.assertIn('Content-Security-Policy', response)

    def test_the_public_emergency_card_sends_no_referrer(self):
        response = self.client.get('/accounts/emergency/00000000-0000-0000-0000-000000000000/')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_csp_reports_are_accepted_and_logged(self):
        with self.assertLogs('healthcompass.csp', level='WARNING'):
            response = self.client.post(
                '/csp-report/', data='{"csp-report": {"violated-directive": "script-src"}}',
                content_type='application/csp-report')
        self.assertEqual(response.status_code, 204)


class SharedCacheCheckTests(SimpleTestCase):
    def test_production_with_locmem_is_an_error(self):
        from apps.accounts.checks import check_shared_cache_in_production
        with override_settings(DEBUG=False, ALLOW_LOCAL_CACHE=False):
            errors = check_shared_cache_in_production(None)
        self.assertEqual([e.id for e in errors], ['accounts.E002'])

    def test_allowed_explicitly_or_with_redis(self):
        from apps.accounts.checks import check_shared_cache_in_production
        with override_settings(DEBUG=False, ALLOW_LOCAL_CACHE=True):
            self.assertEqual(check_shared_cache_in_production(None), [])
        redis = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache',
                             'LOCATION': 'redis://localhost:6379/0'}}
        with override_settings(DEBUG=False, ALLOW_LOCAL_CACHE=False, CACHES=redis):
            self.assertEqual(check_shared_cache_in_production(None), [])


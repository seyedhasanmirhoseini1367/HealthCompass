"""Regression tests for audit SEC-03: anonymous research demo endpoints.

Labs routes (seizure analysis, real-time EEG, ICU demo, model submission) are
404 unless LABS_ENABLED, and require login when it is on.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

WEB_ROUTES = [
    '/insights/seizure/', '/insights/seizure-realtime/', '/insights/seizure-realtime/load/',
    '/insights/seizure-realtime/models/', '/insights/seizure-realtime/predict-chunk/',
    '/insights/icu/', '/insights/submit/',
]
API_ROUTES = ['/api/v1/seizure-analysis/', '/api/v1/icu/', '/api/v1/seizure-realtime/analyze/']


class LabsOffTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(LABS_ENABLED=False)
    def test_every_labs_route_is_404_when_off(self):
        user = get_user_model().objects.create_user('labs_off', email='lo@test.invalid', password='pw')
        for logged_in in (False, True):
            if logged_in:
                self.client.force_login(user)
            for url in WEB_ROUTES:
                with self.subTest(url=url, logged_in=logged_in):
                    self.assertEqual(self.client.post(url).status_code, 404)
        api = APIClient()
        api.force_authenticate(user)
        for url in API_ROUTES:
            with self.subTest(url=url):
                self.assertEqual(api.post(url).status_code, 404)

    @override_settings(LABS_ENABLED=False)
    def test_anonymous_seizure_post_never_reaches_the_third_party(self):
        with patch('requests.post') as outbound:
            response = self.client.post('/insights/seizure/',
                                        {'signal_file': SimpleUploadedFile('e.parquet', b'PAR1')})
        self.assertEqual(response.status_code, 404)
        outbound.assert_not_called()


@override_settings(LABS_ENABLED=True)
class LabsOnTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_anonymous_requests_are_sent_to_login(self):
        for url in WEB_ROUTES:
            with self.subTest(url=url):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login', response['Location'])

    def test_oversize_seizure_upload_is_413_before_reading(self):
        user = get_user_model().objects.create_user('labs_on', email='ln@test.invalid', password='pw')
        self.client.force_login(user)
        with override_settings(MAX_UPLOAD_BYTES=10), patch('requests.post') as outbound:
            response = self.client.post('/insights/seizure/',
                                        {'signal_file': SimpleUploadedFile('e.parquet', b'x' * 100)})
        self.assertEqual(response.status_code, 413)
        outbound.assert_not_called()

    def test_api_oversize_upload_is_413(self):
        user = get_user_model().objects.create_user('labs_api', email='la@test.invalid', password='pw')
        api = APIClient()
        api.force_authenticate(user)
        with override_settings(MAX_UPLOAD_BYTES=10):
            for url in ('/api/v1/seizure-analysis/', '/api/v1/seizure-realtime/analyze/'):
                with self.subTest(url=url):
                    response = api.post(url, {'signal_file': SimpleUploadedFile('e.csv', b'x' * 100)},
                                        format='multipart')
                    self.assertEqual(response.status_code, 413)

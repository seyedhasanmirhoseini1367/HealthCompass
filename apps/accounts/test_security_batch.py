"""
REGRESSION — NEW-06, NEW-07, NEW-14, NEW-15, NEW-17.

Five independent findings, each small, each with a concrete failure mode.
"""
import ast
import pathlib

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.models import PatientProfile
from apps.accounts.views import _get_client_ip


class ClientIpTests(TestCase):
    """
    NEW-06 — X-Forwarded-For is client-controlled.

    Its FIRST element is whatever the caller sent; proxies append rather than
    overwrite. Trusting it let anyone send a fresh value per request and so
    bypass the emergency-card rate limit entirely (30/min became unlimited,
    enabling brute-force enumeration of emergency_token UUIDs) and poison
    EmergencyCardView.ip_hash, destroying the audit trail's value.
    """

    def setUp(self):
        self.factory = RequestFactory()

    def test_forwarded_for_is_not_trusted(self):
        """ACCEPTANCE — NEW-06."""
        request = self.factory.get('/')
        request.META['REMOTE_ADDR'] = '10.0.0.9'
        request.META['HTTP_X_FORWARDED_FOR'] = '1.2.3.4, 10.0.0.1'
        self.assertEqual(_get_client_ip(request), '10.0.0.9')

    def test_spoofed_header_cannot_vary_the_rate_limit_key(self):
        """Every spoofed value must map to the same key."""
        seen = set()
        for spoof in ('1.1.1.1', '2.2.2.2', '3.3.3.3'):
            request = self.factory.get('/')
            request.META['REMOTE_ADDR'] = '10.0.0.9'
            request.META['HTTP_X_FORWARDED_FOR'] = spoof
            seen.add(_get_client_ip(request))
        self.assertEqual(seen, {'10.0.0.9'})

    def test_missing_remote_addr_is_empty_not_an_error(self):
        """RequestFactory supplies 127.0.0.1, so remove it to test the absent case."""
        request = self.factory.get('/')
        request.META.pop('REMOTE_ADDR', None)
        self.assertEqual(_get_client_ip(request), '')


class EmergencyCardDefaultTests(TestCase):
    """
    NEW-07 — disclosure by default.

    Every patient profile got a live, no-login URL exposing name, date of birth,
    blood type, allergies and emergency contacts from the moment it was created,
    before the user had seen the feature or been told a QR code existed. GDPR
    Art. 25 is data protection *by default*.
    """

    def test_new_profiles_are_not_publicly_readable(self):
        """ACCEPTANCE — NEW-07. Was default=True."""
        user = get_user_model().objects.create_user(
            username='ec', password='pw-test-only', email='ec@example.com')
        profile = PatientProfile.objects.create(user=user)
        self.assertFalse(profile.emergency_card_enabled)

    def test_a_disabled_card_is_not_served(self):
        user = get_user_model().objects.create_user(
            username='ec2', password='pw-test-only', email='ec2@example.com')
        profile = PatientProfile.objects.create(user=user)
        response = self.client.get(
            reverse('accounts:emergency_card_public', args=[profile.emergency_token]))
        self.assertEqual(response.status_code, 404)

    def test_the_patient_can_still_opt_in(self):
        """Opt-in must remain possible, or the feature is simply removed."""
        user = get_user_model().objects.create_user(
            username='ec3', password='pw-test-only', email='ec3@example.com')
        profile = PatientProfile.objects.create(user=user, emergency_card_enabled=True)
        response = self.client.get(
            reverse('accounts:emergency_card_public', args=[profile.emergency_token]))
        self.assertEqual(response.status_code, 200)


class MessageLengthTests(TestCase):
    """
    NEW-14 — no cap on chat message length.

    DATA_UPLOAD_MAX_MEMORY_SIZE is 50 MB and does not restrict a single JSON
    field, so a multi-megabyte question was accepted, sent to the provider at
    cost, and stored verbatim in QueryLog.query.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='chat', password='pw-test-only', email='chat@example.com')
        self.client.force_login(self.user)

    def test_oversized_message_is_rejected(self):
        """ACCEPTANCE — NEW-14."""
        from apps.rag_assistant.views import MAX_QUESTION_CHARS
        response = self.client.post(
            reverse('rag_assistant:send_message'),
            data={'message': 'x' * (MAX_QUESTION_CHARS + 1)},
            content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_oversized_message_is_rejected_on_the_streaming_path_too(self):
        from apps.rag_assistant.views import MAX_QUESTION_CHARS
        response = self.client.post(
            reverse('rag_assistant:stream_message'),
            data={'message': 'x' * (MAX_QUESTION_CHARS + 1)},
            content_type='application/json')
        self.assertEqual(response.status_code, 400)


class NewSessionMethodTests(TestCase):
    """
    NEW-15 — a GET that created a database row.

    An <img> tag pointing at /assistant/new/ on any page created a ChatSession
    for every logged-in visitor who loaded it: unbounded, unrated, and
    CSRF-exempt by construction because GET is exempt.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='sess', password='pw-test-only', email='sess@example.com')
        self.client.force_login(self.user)

    def test_get_does_not_create_a_session(self):
        """ACCEPTANCE — NEW-15."""
        from apps.rag_assistant.models import ChatSession
        before = ChatSession.objects.filter(patient=self.user).count()
        response = self.client.get(reverse('rag_assistant:new_session'))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(ChatSession.objects.filter(patient=self.user).count(), before)

    def test_post_still_creates_a_session(self):
        from apps.rag_assistant.models import ChatSession
        before = ChatSession.objects.filter(patient=self.user).count()
        self.client.post(reverse('rag_assistant:new_session'))
        self.assertEqual(ChatSession.objects.filter(patient=self.user).count(), before + 1)


class GlobalRngTests(TestCase):
    """
    NEW-17 — a view reseeded the process-wide RNG.

    Seeding the module-level generator inside a request handler resets the
    sequence for every other caller in the process. Nothing security-relevant
    uses `random` today, which is the only reason this was low severity.
    """

    def test_icu_views_do_not_reseed_the_global_rng(self):
        offenders = []
        for path in (pathlib.Path('apps/ai_insights/views/icu.py'),
                     pathlib.Path('apps/api/views/icu.py')):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'seed'
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'random'):
                    offenders.append(f'{path}:{node.lineno}')
        self.assertEqual(offenders, [],
                         f'use a local Random(seed) instance instead: {offenders}')

    def test_icu_demo_data_is_still_deterministic(self):
        """The fix must not make the demo output vary between requests."""
        from apps.ai_insights.views.icu import _icu_mock_eeg
        self.assertEqual(_icu_mock_eeg(7), _icu_mock_eeg(7))

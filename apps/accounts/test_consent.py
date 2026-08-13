"""
Consent tests.

Consent is the gate on transmitting health data to third-party AI providers, so
the tests are written around the failure modes that matter: default-deny, stale
versions, revocation actually stopping processing, and one user never seeing or
changing another's decisions.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.accounts.consent import (ConsentRequired, consent_history, consent_status,
                                   enforce_for_ai, grant_consent, has_consent,
                                   require_consent, revoke_consent)
from apps.accounts.models import Consent, ConsentPurpose

User = get_user_model()

EXTERNAL = ConsentPurpose.EXTERNAL_LLM
RESEARCH = ConsentPurpose.RESEARCH

V2 = {**{k: 'v1' for k in ConsentPurpose.values}, EXTERNAL: 'v2'}


class ConsentModelTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='cu', email='cu@example.com', password='pw-consent-1',
        )

    def test_no_record_means_no_consent(self):
        """Default-deny: silence is not consent."""
        self.assertFalse(has_consent(self.user, EXTERNAL))

    def test_grant_records_purpose_version_and_time(self):
        c = grant_consent(self.user, EXTERNAL)
        self.assertEqual(c.purpose, EXTERNAL)
        self.assertEqual(c.version, 'v1')
        self.assertEqual(c.status, Consent.Status.GRANTED)
        self.assertIsNotNone(c.granted_at)
        self.assertIsNone(c.revoked_at)
        self.assertTrue(has_consent(self.user, EXTERNAL))

    def test_grant_is_idempotent_at_the_same_version(self):
        first  = grant_consent(self.user, EXTERNAL)
        second = grant_consent(self.user, EXTERNAL)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Consent.objects.filter(user=self.user, purpose=EXTERNAL).count(), 1)

    def test_revoke_stops_consent_but_keeps_the_record(self):
        grant_consent(self.user, EXTERNAL)
        self.assertTrue(revoke_consent(self.user, EXTERNAL))
        self.assertFalse(has_consent(self.user, EXTERNAL))

        record = Consent.objects.get(user=self.user, purpose=EXTERNAL)
        self.assertEqual(record.status, Consent.Status.REVOKED)
        self.assertIsNotNone(record.revoked_at)
        self.assertIsNotNone(record.granted_at, 'the original grant time must survive revocation')

    def test_revoking_without_consent_is_a_no_op(self):
        self.assertFalse(revoke_consent(self.user, EXTERNAL))

    def test_regranting_after_revocation_creates_a_new_record(self):
        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        grant_consent(self.user, EXTERNAL)

        rows = Consent.objects.filter(user=self.user, purpose=EXTERNAL)
        self.assertEqual(rows.count(), 2, 'history must not be overwritten by re-granting')
        self.assertEqual(rows.filter(status=Consent.Status.GRANTED).count(), 1)
        self.assertTrue(has_consent(self.user, EXTERNAL))

    def test_purposes_are_independent(self):
        """No blanket 'I agree' — granting one purpose grants only that one."""
        grant_consent(self.user, RESEARCH)
        self.assertTrue(has_consent(self.user, RESEARCH))
        self.assertFalse(has_consent(self.user, EXTERNAL))

    def test_unknown_purpose_is_rejected(self):
        with self.assertRaises(ValueError):
            grant_consent(self.user, 'not_a_real_purpose')
        with self.assertRaises(ValueError):
            revoke_consent(self.user, 'not_a_real_purpose')

    def test_anonymous_user_never_has_consent(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(has_consent(AnonymousUser(), EXTERNAL))
        self.assertFalse(has_consent(None, EXTERNAL))

    def test_only_one_active_consent_per_purpose(self):
        from django.db import IntegrityError, transaction

        grant_consent(self.user, EXTERNAL)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Consent.objects.create(
                    user=self.user, purpose=EXTERNAL, version='v1',
                    status=Consent.Status.GRANTED,
                )


class ConsentVersioningTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='vu', email='vu@example.com', password='pw-version-1',
        )

    def test_consent_at_an_old_version_does_not_count(self):
        grant_consent(self.user, EXTERNAL)          # v1
        self.assertTrue(has_consent(self.user, EXTERNAL))

        with override_settings(CONSENT_VERSIONS=V2):
            self.assertFalse(
                has_consent(self.user, EXTERNAL),
                'consent to superseded wording must not carry over',
            )

    def test_status_flags_a_stale_consent_for_reconsent(self):
        grant_consent(self.user, EXTERNAL)
        with override_settings(CONSENT_VERSIONS=V2):
            row = next(r for r in consent_status(self.user) if r['purpose'] == EXTERNAL)
        self.assertFalse(row['granted'])
        self.assertTrue(row['needs_reconsent'])
        self.assertEqual(row['granted_version'], 'v1')
        self.assertEqual(row['current_version'], 'v2')

    def test_regranting_at_a_new_version_revokes_the_old_record(self):
        grant_consent(self.user, EXTERNAL)
        with override_settings(CONSENT_VERSIONS=V2):
            grant_consent(self.user, EXTERNAL)
            self.assertTrue(has_consent(self.user, EXTERNAL))

        # Ordered by pk, not created_at: both rows can land in the same
        # auto_now_add tick, which makes a timestamp sort nondeterministic.
        rows = list(Consent.objects.filter(user=self.user, purpose=EXTERNAL).order_by('pk'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].version, 'v1')
        self.assertEqual(rows[0].status, Consent.Status.REVOKED)
        self.assertIsNotNone(rows[0].revoked_at)
        self.assertEqual(rows[1].version, 'v2')
        self.assertEqual(rows[1].status, Consent.Status.GRANTED)

    def test_history_retains_every_decision(self):
        """
        Revocation stamps the existing row rather than appending one, so a
        grant→revoke cycle is a single record carrying both timestamps. A
        subsequent re-grant is what adds a row.
        """
        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        grant_consent(self.user, RESEARCH)

        history = consent_history(self.user)
        self.assertEqual(len(history), 2)

        external = next(h for h in history if h.purpose == EXTERNAL)
        self.assertIsNotNone(external.granted_at)
        self.assertIsNotNone(external.revoked_at)
        self.assertEqual(external.status, Consent.Status.REVOKED)

        grant_consent(self.user, EXTERNAL)
        self.assertEqual(len(consent_history(self.user)), 3)


class ConsentEnforcementTests(TestCase):
    """enforce_for_ai() is the gate in front of every external LLM call."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='eu', email='eu@example.com', password='pw-enforce-1',
        )

    def test_missing_consent_raises(self):
        with self.assertRaises(ConsentRequired) as ctx:
            enforce_for_ai(self.user)
        self.assertEqual(ctx.exception.purpose, EXTERNAL)

    def test_granted_consent_passes(self):
        grant_consent(self.user, EXTERNAL)
        enforce_for_ai(self.user)   # must not raise

    def test_revoked_consent_blocks_again(self):
        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        with self.assertRaises(ConsentRequired):
            enforce_for_ai(self.user)

    def test_stale_version_blocks(self):
        grant_consent(self.user, EXTERNAL)
        with override_settings(CONSENT_VERSIONS=V2):
            with self.assertRaises(ConsentRequired):
                enforce_for_ai(self.user)

    def test_denial_message_names_the_external_providers(self):
        with self.assertRaises(ConsentRequired) as ctx:
            require_consent(self.user, EXTERNAL)
        self.assertIn('external AI providers', ctx.exception.message)

    @override_settings(CONSENT_REQUIRED_PURPOSES=[])
    def test_enforcement_set_is_configurable(self):
        enforce_for_ai(self.user)   # nothing required → must not raise


class AIPipelineConsentTests(TestCase):
    """
    The gate must sit inside RAGService, not in the views — ask() and
    stream_ask() are the only two doors into the pipeline.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='ru', email='ru@example.com', password='pw-rag-1',
        )

    def test_ask_is_blocked_without_consent(self):
        from apps.rag_assistant.services.rag_service import RAGService

        answer, sources, provider, chunks, safety, rules = RAGService().ask(self.user, 'hi')
        self.assertEqual(provider, 'consent_required')
        self.assertIn('consent_required', rules)
        self.assertEqual(sources, [])
        self.assertEqual(chunks, 0)
        self.assertIn('consent', answer.lower())

    def test_ask_does_not_call_any_external_provider_without_consent(self):
        """The point of the gate: no PHI leaves the system."""
        from unittest.mock import patch
        from apps.rag_assistant.services.rag_service import RAGService

        with patch('apps.rag_assistant.graph.graph.stream_graph') as mock_graph:
            RAGService().ask(self.user, 'what is my LDL?')
        mock_graph.assert_not_called()

    def test_stream_ask_is_blocked_without_consent(self):
        from apps.rag_assistant.services.rag_service import RAGService

        events = ''.join(RAGService().stream_ask(self.user, 'hi'))
        self.assertIn('consent_required', events)
        self.assertIn('"type": "done"', events)

    def test_stream_ask_does_not_reach_the_pipeline_without_consent(self):
        from unittest.mock import patch
        from apps.rag_assistant.services.rag_service import RAGService

        with patch('apps.rag_assistant.graph.graph.stream_graph') as mock_graph:
            list(RAGService().stream_ask(self.user, 'what is my LDL?'))
        mock_graph.assert_not_called()

    def test_pipeline_runs_once_consent_is_granted(self):
        from unittest.mock import patch
        from apps.rag_assistant.services.rag_service import RAGService

        grant_consent(self.user, EXTERNAL)
        with patch('apps.rag_assistant.graph.graph.stream_graph', return_value=iter([])) as mock_graph:
            RAGService().ask(self.user, 'what is my LDL?')
        mock_graph.assert_called_once()

    def test_revoking_consent_blocks_the_pipeline_again(self):
        from unittest.mock import patch
        from apps.rag_assistant.services.rag_service import RAGService

        grant_consent(self.user, EXTERNAL)
        revoke_consent(self.user, EXTERNAL)
        with patch('apps.rag_assistant.graph.graph.stream_graph') as mock_graph:
            RAGService().ask(self.user, 'what is my LDL?')
        mock_graph.assert_not_called()


class ConsentWebViewTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='wu', email='wu@example.com', password='pw-web-consent-1',
        )
        self.client.force_login(self.user)

    def test_page_lists_every_purpose(self):
        resp = self.client.get(reverse('accounts:consent'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        for _, label in ConsentPurpose.choices:
            self.assertIn(label, body)

    def test_grant_and_revoke_through_the_page(self):
        self.client.post(reverse('accounts:consent'),
                         {'purpose': EXTERNAL, 'action': 'grant'})
        self.assertTrue(has_consent(self.user, EXTERNAL))

        self.client.post(reverse('accounts:consent'),
                         {'purpose': EXTERNAL, 'action': 'revoke'})
        self.assertFalse(has_consent(self.user, EXTERNAL))

    def test_unknown_purpose_is_rejected_without_error(self):
        resp = self.client.post(reverse('accounts:consent'),
                                {'purpose': 'nonsense', 'action': 'grant'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Consent.objects.count(), 0)

    def test_page_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse('accounts:consent'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)


class ConsentAPITests(APITestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='au', email='au@example.com', password='pw-api-consent-1',
        )
        self.other = User.objects.create_user(
            username='ou', email='ou@example.com', password='pw-other-consent-1',
        )
        self.client.force_authenticate(user=self.user)

    def test_list_returns_every_purpose(self):
        resp = self.client.get('/api/v1/consent/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['consents']), len(ConsentPurpose.choices))

    def test_grant_then_revoke(self):
        resp = self.client.post('/api/v1/consent/grant/', {'purpose': EXTERNAL}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(has_consent(self.user, EXTERNAL))

        resp = self.client.post('/api/v1/consent/revoke/', {'purpose': EXTERNAL}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['revoked'])
        self.assertFalse(has_consent(self.user, EXTERNAL))

    def test_unknown_purpose_returns_400(self):
        resp = self.client.post('/api/v1/consent/grant/', {'purpose': 'nope'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_endpoints_require_authentication(self):
        self.client.force_authenticate(user=None)
        for method, url in [('get',  '/api/v1/consent/'),
                            ('post', '/api/v1/consent/grant/'),
                            ('post', '/api/v1/consent/revoke/'),
                            ('get',  '/api/v1/consent/history/')]:
            with self.subTest(url=url):
                self.assertIn(getattr(self.client, method)(url).status_code, (401, 403))

    def test_cannot_read_another_users_consent(self):
        """There is no user parameter — a caller only ever sees their own."""
        grant_consent(self.other, EXTERNAL)
        resp = self.client.get('/api/v1/consent/')
        row = next(r for r in resp.json()['consents'] if r['purpose'] == EXTERNAL)
        self.assertFalse(row['granted'])

    def test_cannot_grant_consent_on_behalf_of_another_user(self):
        resp = self.client.post(
            '/api/v1/consent/grant/',
            {'purpose': EXTERNAL, 'user': self.other.pk, 'user_id': self.other.pk},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(has_consent(self.user, EXTERNAL))
        self.assertFalse(
            has_consent(self.other, EXTERNAL),
            'a supplied user id must never redirect the consent decision',
        )

    def test_cannot_revoke_another_users_consent(self):
        grant_consent(self.other, EXTERNAL)
        self.client.post('/api/v1/consent/revoke/',
                         {'purpose': EXTERNAL, 'user_id': self.other.pk}, format='json')
        self.assertTrue(has_consent(self.other, EXTERNAL))

    def test_history_only_shows_the_callers_own_decisions(self):
        grant_consent(self.other, EXTERNAL)
        grant_consent(self.user, RESEARCH)
        history = self.client.get('/api/v1/consent/history/').json()['history']
        self.assertEqual([h['purpose'] for h in history], [RESEARCH])

    def test_assistant_is_blocked_then_unblocked_by_consent(self):
        resp = self.client.post('/api/v1/assistant/ask/', {'query': 'hi'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('consent', resp.json()['answer'].lower())

        self.client.post('/api/v1/consent/grant/', {'purpose': EXTERNAL}, format='json')
        self.assertTrue(has_consent(self.user, EXTERNAL))

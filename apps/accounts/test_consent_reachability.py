"""
The consent page has to be reachable, and the refusal has to lead to it.

`enforce_for_ai` refuses the assistant without EXTERNAL_LLM consent and tells
the patient they can grant or withdraw it "in Privacy & Consent settings". That
page existed at /accounts/consent/ and **nothing in the UI linked to it** — not
the navbar, not the profile menu, not the refusal itself. A patient could only
reach it by being told the URL.

That is worse than an ordinary dead end. Consent is a control the patient is
legally entitled to exercise in both directions, and the withdrawal path had the
same gap: someone who wanted to stop their records going to external providers
could not find where to say so.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class ConsentPageReachabilityTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'consent_nav', email='consent_nav@test.invalid', password='pw',
            role='patient')
        self.client.force_login(self.patient)

    def test_the_page_answers(self):
        response = self.client.get(reverse('accounts:consent'))
        self.assertEqual(response.status_code, 200)

    def test_a_signed_in_patient_is_given_a_link_to_it(self):
        """ACCEPTANCE. No template referenced the page at all."""
        url = reverse('accounts:consent')
        response = self.client.get('/dashboard/')
        self.assertContains(response, f'href="{url}"',
                            msg_prefix='no link to Privacy & Consent in the chrome')

    def test_a_phone_user_can_still_reach_it(self):
        """
        Phone users navigate through a different menu, and must still get there.

        This used to count the link twice in the page — a proxy for "it is in
        both menus" that stopped being true when navigation was consolidated to
        six entries and the mobile menu stopped listing every capability.

        The proxy was wrong; the guarantee was not. So the path is walked
        instead: the mobile menu offers Settings, and Settings offers consent.
        That is a stronger assertion than counting occurrences — it fails if
        either half of the route breaks, including if the Settings page stops
        linking consent.
        """
        body = self.client.get('/dashboard/').content.decode()
        mobile = body[body.index('id="mobileMenu"'):body.index('</nav>')]
        self.assertIn('href="/dashboard/settings/"', mobile,
                      'the phone menu has no route towards consent')

        settings_page = self.client.get('/dashboard/settings/')
        self.assertContains(settings_page, reverse('accounts:consent'))

    def test_anonymous_visitors_are_not_offered_it(self):
        """There is no consent to manage without an account."""
        self.client.logout()
        response = self.client.get('/')
        self.assertNotContains(response, reverse('accounts:consent'))


class ConsentRefusalIsActionableTests(TestCase):
    """The refusal must carry the way to act on it, not just name it."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'consent_refused', email='consent_refused@test.invalid', password='pw',
            role='patient')

    def _stream(self):
        import json

        from apps.rag_assistant.services.rag_service import RAGService

        events = []
        for chunk in RAGService().stream_ask('What do my labs show?', self.patient):
            for line in chunk.splitlines():
                if line.startswith('data: '):
                    try:
                        events.append(json.loads(line[6:]))
                    except json.JSONDecodeError:
                        pass
        return events

    def test_the_refusal_is_delivered_as_a_normal_answer(self):
        events = self._stream()
        kinds = [e.get('type') for e in events]
        self.assertIn('token', kinds)
        self.assertIn('done', kinds)

    def test_the_refusal_explains_itself(self):
        events = self._stream()
        text = ''.join(e.get('content', '') for e in events if e.get('type') == 'token')
        self.assertIn('consent', text.lower())

    def test_the_refusal_carries_the_consent_url(self):
        """ACCEPTANCE. The patient was told where to go and given no way there."""
        meta = next(e for e in self._stream() if e.get('type') == 'meta')
        self.assertEqual(meta.get('mode'), 'consent_required')
        self.assertEqual(meta.get('consent_url'), reverse('accounts:consent'))

    def test_the_url_is_a_same_origin_path(self):
        """
        The client renders it as a button the patient is told to trust, so it
        must not be able to point off-site.
        """
        meta = next(e for e in self._stream() if e.get('type') == 'meta')
        url = meta['consent_url']
        self.assertTrue(url.startswith('/'), url)
        self.assertFalse(url.startswith('//'), url)

    def test_granting_consent_lets_the_assistant_run(self):
        """The link has to actually resolve the refusal, or it is decoration."""
        from apps.accounts.consent import ConsentRequired, enforce_for_ai
        from apps.accounts.models import ConsentPurpose

        with self.assertRaises(ConsentRequired):
            enforce_for_ai(self.patient)

        self.client.force_login(self.patient)
        self.client.post(reverse('accounts:consent'), {
            'purpose': ConsentPurpose.EXTERNAL_LLM, 'action': 'grant'})

        enforce_for_ai(self.patient)   # must not raise

    def test_consent_can_be_withdrawn_from_the_same_page(self):
        """Withdrawal had the same dead end, and matters more than granting."""
        from apps.accounts.consent import ConsentRequired, enforce_for_ai
        from apps.accounts.models import ConsentPurpose

        self.client.force_login(self.patient)
        self.client.post(reverse('accounts:consent'), {
            'purpose': ConsentPurpose.EXTERNAL_LLM, 'action': 'grant'})
        enforce_for_ai(self.patient)

        self.client.post(reverse('accounts:consent'), {
            'purpose': ConsentPurpose.EXTERNAL_LLM, 'action': 'revoke'})
        with self.assertRaises(ConsentRequired):
            enforce_for_ai(self.patient)

"""
Smoke tests — every GET-able URL must not return 5xx.

Why this file exists
--------------------
`apps/accounts/views.py emergency_card()` imported `qrcode`, which was never
declared in requirements.txt. On a clean build the view raised ImportError and
returned 500. It worked in development only because a dev virtualenv had the
package from an earlier unpinned install, and **no test touched that view**, so
the suite was green while a production page was broken.

A per-view test would only have caught the dependency we already knew about.
This sweep catches the class: any URL that 500s for any reason — a missing
runtime dependency, a template that references a removed context variable, a
view importing a module that was renamed — fails here.

It is deliberately shallow. It asserts "did not blow up", not correctness.
2xx/3xx/4xx are all acceptable; only 5xx is a failure. Authorization is tested
properly elsewhere (test_security.py, test_appsec.py); a 403 here is a pass.

Excluded, with reasons
----------------------
* streaming/SSE endpoints — they hold a connection open and call providers
* pure-POST endpoints — a GET is not meaningful
* URLs whose parameters cannot be synthesised (arbitrary slugs/uuids)
"""
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse

#: Endpoints a GET sweep must not touch.
SKIP_NAMES = {
    # SSE / streaming — hold the connection, call providers.
    'rag_assistant:stream_message', 'api:assistant_stream',
    # Explicitly POST-only or state-destructive.
    'accounts:revoke_emergency_token', 'accounts:toggle_emergency_card',
    'accounts:logout', 'api:token_refresh',
    # Debug/plaintext dumps.
    'ai_insights:debug_handlers',
    # allauth Google endpoints raise DoesNotExist without a configured
    # SocialApp row. Production has one (startup.sh runs ensure_social_app);
    # the test database does not, so a failure here would be an artefact of the
    # environment rather than a defect. Google sign-in is covered separately.
    'google_login', 'google_callback', 'google_login_by_token',
}

#: Placeholder arguments for parameterised URLs. A row that does not exist is
#: fine: 404 is a pass, 500 is not.
_ARG_SAMPLES = {
    'pk':         str(uuid.uuid4()),
    'slug':       'nonexistent-slug',
    'token':      str(uuid.uuid4()),
    'session_id': str(uuid.uuid4()),
    'record_id':  str(uuid.uuid4()),
}


def _iter_named_patterns(resolver=None, prefix=''):
    """Yield (namespaced_name, pattern) for every named URL in the project."""
    resolver = resolver or get_resolver()
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            ns = f'{prefix}{entry.namespace}:' if entry.namespace else prefix
            yield from _iter_named_patterns(entry, ns)
        elif isinstance(entry, URLPattern) and entry.name:
            yield f'{prefix}{entry.name}', entry


class UrlSmokeTests(TestCase):
    """No GET-able URL may return 5xx for an authenticated patient."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username='smoke-patient', password='pw-test-only',
            email='smoke@example.com', role='patient')

    def setUp(self):
        self.client.force_login(self.user)

    def _resolve(self, name, pattern):
        """Build a URL, supplying placeholder args where the pattern needs them."""
        try:
            return reverse(name)
        except Exception:
            pass
        keys = list(getattr(pattern.pattern, 'converters', {}).keys())
        if not keys:
            return None
        kwargs = {k: _ARG_SAMPLES.get(k, '1') for k in keys}
        try:
            return reverse(name, kwargs=kwargs)
        except Exception:
            return None

    def test_no_url_returns_server_error(self):
        checked, failures = 0, []

        for name, pattern in _iter_named_patterns():
            if name in SKIP_NAMES:
                continue
            url = self._resolve(name, pattern)
            if url is None:
                continue
            try:
                response = self.client.get(url)
            except Exception as exc:
                # An exception escaping the view is the same defect class as a
                # 500 — ImportError surfaces exactly this way under the test
                # client, which is how the qrcode bug reached production.
                failures.append(f'{name} ({url}) raised {type(exc).__name__}: {exc}')
                continue
            checked += 1
            if response.status_code >= 500:
                failures.append(f'{name} ({url}) -> {response.status_code}')

        self.assertGreater(checked, 20, 'smoke sweep resolved implausibly few URLs')
        self.assertEqual(failures, [], 'URLs returning 5xx:\n  ' + '\n  '.join(failures))


class EmergencyCardTests(TestCase):
    """The specific view whose missing dependency this file was written for."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='qr-patient', password='pw-test-only',
            email='qr@example.com', role='patient')
        self.client.force_login(self.user)

    def test_emergency_card_renders_a_qr_code(self):
        """
        ACCEPTANCE — NEW-04. Fails with ImportError if `qrcode` is not installed,
        which is precisely what happened on every clean build.
        """
        response = self.client.get(reverse('accounts:emergency_card'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['qr_b64'],
                        'qr_b64 must be populated — the QR image is the feature')

    def test_qrcode_is_a_declared_dependency(self):
        """A runtime import that is not declared is a deploy-time 500."""
        import pathlib

        requirements = (pathlib.Path(__file__).resolve().parents[2]
                        / 'requirements.txt').read_text(encoding='utf-8')
        self.assertRegex(requirements, r'(?m)^qrcode==',
                         'qrcode is imported by emergency_card() and must be pinned')

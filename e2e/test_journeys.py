"""
Browser journeys (audit T-02, T-03, prompt 9 task 3).

Real Chromium (Playwright) against Django's live server, with the AI providers
stubbed in-process. Every page visited is checked for JavaScript errors and run
through axe-core; serious or critical accessibility violations fail the test.

Run:  python manage.py test e2e        (needs `playwright install chromium`)
"""
import json
import os
from unittest import mock, skipUnless

os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')  # Playwright's loop shares the thread

from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402
from django.test import override_settings  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    HAVE_PLAYWRIGHT = False

PASSWORD = 'Correct-Horse-Battery-77'
XSS = '<img src=x onerror="window.__xss=1">'


def _stream(*_a, **_k):
    yield 'data: ' + json.dumps({'type': 'token', 'content': 'Your haemoglobin is 140 g/L.'}) + '\n\n'
    yield 'data: ' + json.dumps({'type': 'sources', 'sources': [
        {'title': 'Lab result', 'record_id': None, 'snippet': 'Hb 140'}]}) + '\n\n'
    yield 'data: ' + json.dumps({'type': 'meta', 'provider': 'stub', 'chunks': 1, 'mode': 'personal'}) + '\n\n'
    yield 'data: {"type": "done"}\n\n'


@skipUnless(HAVE_PLAYWRIGHT, 'playwright is not installed')
@override_settings(RATELIMIT_ENABLE=False, MFA_ENFORCED=False, SECURE_SSL_REDIRECT=False)
class JourneyTests(StaticLiveServerTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.js_errors = []
        self.page.on('pageerror', lambda exc: self.js_errors.append(str(exc)))
        self.page.on('console', lambda msg: msg.type == 'error' and self.js_errors.append(msg.text))
        self.visited = []

    def tearDown(self):
        self.context.close()

    # ── helpers ──────────────────────────────────────────────────────────────
    def go(self, path):
        self.page.goto(self.live_server_url + path)
        self.page.wait_for_load_state('networkidle')
        self.check_page(path)

    maxDiff = None

    def check_page(self, label):
        # Images the stub data references do not exist; only script errors count.
        errors = [e for e in self.js_errors if 'Failed to load resource' not in e]
        self.assertEqual(errors, [], f'JavaScript errors on {label}')
        self.js_errors.clear()
        from axe_playwright_python.sync_playwright import Axe
        results = Axe().run(self.page)
        bad = [v for v in results.response['violations'] if v.get('impact') in ('serious', 'critical')]
        detail = [(v['id'], v['impact'], [n['target'] for n in v['nodes']][:5],
                   (v['nodes'][0].get('failureSummary') or '')[:160]) for v in bad]
        self.assertEqual(detail, [], f'axe violations on {label}')
        self.visited.append(label)

    def register(self, username, email):
        self.go('/accounts/register/')
        self.page.fill('#id_username', username)
        self.page.fill('#id_email', email)
        self.page.fill('#id_password1', PASSWORD)
        self.page.fill('#id_password2', PASSWORD)
        self.page.click('main form button[type=submit]')
        self.page.wait_for_load_state('networkidle')

    def login(self, email):
        self.go('/accounts/login/')
        self.page.fill('#id_username', email)
        self.page.fill('#id_password', PASSWORD)
        self.page.click('main form button[type=submit]')
        self.page.wait_for_load_state('networkidle')

    def grant_consent(self, user):
        from apps.accounts.consent import grant_consent
        from apps.accounts.models import ConsentPurpose
        for purpose in ConsentPurpose.values:
            grant_consent(user, purpose)

    # ── journeys ─────────────────────────────────────────────────────────────
    def test_register_consent_records_assistant_export_delete(self):
        from django.contrib.auth import get_user_model
        self.register('journey', 'journey@test.invalid')
        self.assertIn('/dashboard', self.page.url)
        self.check_page('after sign-up')
        user = get_user_model().objects.get(email='journey@test.invalid')

        self.go('/accounts/consent/')
        self.grant_consent(user)

        # a record by pasted text
        self.go('/records/upload/text/')
        self.page.fill('textarea[name=text]', 'Hb 140 g/L (ref 117-155)')
        self.page.click('main form button[type=submit]')
        self.page.wait_for_load_state('networkidle')
        self.check_page('record from text')
        self.go('/records/')
        self.assertIn('Hb', self.page.content())

        # ask a question and see the streamed answer with a source chip
        with mock.patch('apps.rag_assistant.services.rag_service.RAGService.stream_ask', _stream):
            self.go('/assistant/')
            self.page.fill('#msgInput', 'What is my haemoglobin?')
            self.page.click('#sendBtn')
            self.page.wait_for_selector('text=Your haemoglobin is 140 g/L.')
        self.check_page('assistant answer')

        # export
        self.go('/accounts/export/')
        with self.page.expect_download() as download:
            self.page.click('main form button[type=submit]')
        self.assertTrue(download.value.suggested_filename.endswith('.zip'))

        # delete the account through the confirmation dialog page
        self.go('/accounts/delete/')
        self.page.fill('#password', PASSWORD)
        self.page.click('main form button[type=submit]')
        self.page.wait_for_load_state('networkidle')
        self.assertFalse(get_user_model().objects.filter(email='journey@test.invalid').exists())

    def test_share_accept_view_revoke(self):
        from django.contrib.auth import get_user_model

        from apps.testing.factories import make_record_with_labs
        User = get_user_model()
        owner = User.objects.create_user('owner', email='owner@test.invalid', password=PASSWORD)
        User.objects.create_user('friend', email='friend@test.invalid', password=PASSWORD)
        make_record_with_labs(owner, title='Owner lab')

        self.login('owner@test.invalid')
        self.go('/care/')
        if not self.page.is_visible('#share-recipient'):   # open by default with no shares
            self.page.click('text=Share with someone')
        self.page.fill('#share-recipient', 'friend@test.invalid')
        self.page.check('input[name=scopes][value=records]')
        self.page.click('#share-recipient >> xpath=ancestor::form//button[@type="submit"]')
        self.page.wait_for_load_state('networkidle')
        self.check_page('share offered')

        self.context.clear_cookies()
        self.login('friend@test.invalid')
        self.go('/care/')
        self.page.click('text=Accept')
        self.page.wait_for_load_state('networkidle')
        self.go(f'/accounts/shared/{owner.pk}/')
        self.assertIn('Owner lab', self.page.content())

        self.context.clear_cookies()
        self.login('owner@test.invalid')
        self.go('/care/')
        self.page.click('text=Stop sharing')
        self.page.click('#deleteModalConfirm')   # the native <dialog>
        self.page.wait_for_load_state('networkidle')
        from apps.accounts.models import SharingGrant
        self.assertEqual(SharingGrant.objects.get().status, SharingGrant.Status.REVOKED)

    def test_a_hostile_parameter_name_never_runs_script(self):
        """SEC-01 regression in a real browser: the name is written straight to the
        database, bypassing ingestion's normalisation, so the page sinks are tested."""
        from django.contrib.auth import get_user_model

        from apps.medical_records.models import MedicalRecord, ParsedLabValue
        User = get_user_model()
        staff = User.objects.create_superuser('staffer', email='staff@test.invalid', password=PASSWORD)
        for i in range(6):
            p = User.objects.create_user(f'pop{i}', email=f'pop{i}@test.invalid', password=PASSWORD)
            for month in (1, 2):
                r = MedicalRecord.objects.create(patient=p, title='lab', record_date=f'2026-0{month}-10')
                ParsedLabValue.objects.create(record=r, parameter_name=XSS, value=str(5 + i), unit='mmol/L')
        for email, pages in (('pop0@test.invalid', ['/insights/health/', '/insights/analytics/', '/records/']),
                             ('staff@test.invalid', ['/insights/population/'])):
            self.context.clear_cookies()
            self.login(email)
            for path in pages:
                self.go(path)
                self.assertIsNone(self.page.evaluate('window.__xss'), f'script ran on {path}')
        del staff

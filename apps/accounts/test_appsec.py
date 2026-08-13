"""
Application security regression tests.

Each class corresponds to a finding from the hardening audit. Every test asserts
the security property itself — what reaches the browser, what is stored, what
leaves the process — rather than a status code, because a 200 says nothing about
whether a payload survived.
"""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.medical_records.models import MedicalRecord, ParsedLabValue

User = get_user_model()

NO_AUTOINDEX = override_settings(RAG_AUTO_INDEX_SYNC=False)

#: Breaks out of a <script> block even when the value is JSON-encoded, because
#: json.dumps does not escape the ASCII sequence "</script>".
SCRIPT_BREAKOUT = '</script><script>window.__xss=1</script>'


@NO_AUTOINDEX
class StoredXssInChartDataTests(TestCase):
    """
    Lab parameter names come from uploaded documents and are rendered into
    inline <script> blocks as JSON. json.dumps does not escape `</script>`, so
    an unescaped embed lets a lab name terminate the script element early.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='xss', email='xss@example.com', password='pw-xss-1',
        )
        record = MedicalRecord.objects.create(
            patient=self.user, title='Labs', record_type='lab_result',
            record_date=timezone.now().date(),
        )
        for i in range(3):
            ParsedLabValue.objects.create(
                record=record, parameter_name=SCRIPT_BREAKOUT,
                value=str(10 + i), unit='mmol/L', canonical_value=10.0 + i,
                measured_at=timezone.now(),
            )
        self.client.force_login(self.user)

    def test_analytics_page_does_not_let_lab_names_close_the_script_tag(self):
        resp = self.client.get('/insights/analytics/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()

        # The payload may legitimately appear, but only in a neutralised form:
        # never with raw angle brackets that the HTML parser would act on.
        self.assertNotIn('</script><script>window.__xss=1</script>', body)
        self.assertNotIn('<script>window.__xss=1', body)
        # It should still be present, escaped, proving the value was rendered
        # rather than silently dropped (which would pass vacuously).
        self.assertIn('window.__xss=1', body)
        self.assertIn('\\u003C', body)

    def test_breakout_sequence_never_appears_verbatim(self):
        resp = self.client.get('/insights/analytics/')
        body = resp.content.decode()
        # The literal characters that would end the script element must not
        # survive anywhere in the rendered page.
        self.assertNotIn('</script><script>', body)

    def test_health_dashboard_is_also_safe(self):
        resp = self.client.get('/insights/health/')
        if resp.status_code == 200:
            self.assertNotIn('</script><script>', resp.content.decode())


@NO_AUTOINDEX
class ChatMarkdownXssTests(TestCase):
    """
    Assistant answers are rendered client-side with marked() into innerHTML.
    marked passes raw HTML through by default, so HTML inside an answer — which
    can originate from an uploaded document echoed back by the model — would
    execute. The page must not hand raw HTML to the renderer.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='chat', email='chat@example.com', password='pw-chat-1',
        )
        self.client.force_login(self.user)

    def test_chat_page_escapes_html_before_markdown_rendering(self):
        from apps.rag_assistant.models import ChatSession, QueryLog

        session = ChatSession.objects.create(patient=self.user, title='s')
        QueryLog.objects.create(
            session=session, query='hi',
            response='<img src=x onerror="window.__xss=1">',
            sources=[],
        )
        resp = self.client.get(reverse('rag_assistant:chat'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()

        # Django escapes the attribute, so the raw tag must not appear...
        self.assertNotIn('<img src=x onerror', body)
        # ...and the page must escape again before marked() sees it.
        self.assertIn('escapeHtml', body,
                      'chat must neutralise HTML before handing text to marked()')


@NO_AUTOINDEX
class ProfilePictureUploadTests(TestCase):
    """
    profile_picture_upload trusted the client-declared Content-Type. An SVG
    (or anything) announced as image/png was stored verbatim and later served
    from the app's own origin — script in an SVG then runs as first-party.
    """

    SVG_PAYLOAD = (b'<svg xmlns="http://www.w3.org/2000/svg" onload="window.__xss=1">'
                   b'<script>window.__xss=1</script></svg>')
    PNG_HEADER = b'\x89PNG\r\n\x1a\n' + b'0' * 128

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='pic', email='pic@example.com', password='pw-pic-1',
        )
        self.client.force_login(self.user)

    def _post(self, name, content, content_type):
        return self.client.post(
            reverse('accounts:profile_edit'),
            {'profile_picture': SimpleUploadedFile(name, content, content_type=content_type)},
        )

    def test_svg_declared_as_png_is_rejected_by_the_api(self):
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(user=self.user)
        resp = api.post(
            '/api/v1/auth/profile/picture/',
            {'profile_picture': SimpleUploadedFile(
                'evil.svg', self.SVG_PAYLOAD, content_type='image/png')},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_picture,
                         'an SVG must never be stored as a profile picture')

    def test_svg_with_svg_content_type_is_rejected(self):
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(user=self.user)
        resp = api.post(
            '/api/v1/auth/profile/picture/',
            {'profile_picture': SimpleUploadedFile(
                'evil.svg', self.SVG_PAYLOAD, content_type='image/svg+xml')},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_picture)

    def test_genuine_png_is_still_accepted(self):
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(user=self.user)
        resp = api.post(
            '/api/v1/auth/profile/picture/',
            {'profile_picture': SimpleUploadedFile(
                'me.png', self.PNG_HEADER, content_type='image/png')},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.profile_picture)


@NO_AUTOINDEX
class MediaServingTests(TestCase):
    """Uploaded files must never be rendered as active content by the browser."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='media', email='media@example.com', password='pw-media-1',
        )
        self.record = MedicalRecord.objects.create(
            patient=self.user, title='Scan', record_type='imaging',
            file=SimpleUploadedFile('report.pdf', b'%PDF-1.4 content'),
        )
        self.client.force_login(self.user)

    def test_media_response_is_sandboxed_and_nosniff(self):
        resp = self.client.get(f'/media/{self.record.file.name}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertIn('sandbox', resp.headers.get('Content-Security-Policy', ''))
        # A PDF stays viewable; the disposition is explicit either way.
        self.assertTrue(resp.headers.get('Content-Disposition'))

    def test_svg_is_never_served_as_an_active_content_type(self):
        """The core rule: an upload must not come back as something scriptable."""
        for ext, stored in (('.svg', 'evil.svg'), ('.html', 'evil.html')):
            with self.subTest(ext=ext):
                self.record.file.name = f'medical_records/2026/01/{stored}'
                self.record.save(update_fields=['file'])
                # Write the blob so the path exists.
                from django.core.files.storage import default_storage
                default_storage.save(self.record.file.name,
                                     __import__('io').BytesIO(b'<svg onload=1></svg>'))

                resp = self.client.get(f'/media/{self.record.file.name}')
                if resp.status_code != 200:
                    continue
                ctype = resp.headers.get('Content-Type', '').lower()
                self.assertNotIn('svg', ctype)
                self.assertNotIn('html', ctype)
                self.assertEqual(ctype, 'application/octet-stream')
                self.assertIn('attachment', resp.headers.get('Content-Disposition', ''))


@NO_AUTOINDEX
class UploadLimitTests(APITestCase):
    """
    Django's DATA_UPLOAD_MAX_MEMORY_SIZE explicitly excludes file uploads, so
    without an explicit check an upload is unbounded.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='big', email='big@example.com', password='pw-big-1',
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        cache.clear()

    @override_settings(MAX_UPLOAD_BYTES=2048)
    def test_oversized_upload_is_rejected(self):
        payload = b'%PDF-1.4' + b'A' * 8192
        resp = self.client.post(
            '/api/v1/records/upload/pdf/',
            {'pdf_file': SimpleUploadedFile('big.pdf', payload, content_type='application/pdf')},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('too large', resp.json()['error'].lower())
        self.assertFalse(MedicalRecord.objects.filter(patient=self.user).exists())

    @override_settings(MAX_UPLOAD_BYTES=1024 * 1024)
    def test_normal_medical_pdf_still_accepted(self):
        payload = b'%PDF-1.4' + b'A' * 4096
        with patch('apps.medical_records.parsers.PDFParser.parse',
                   return_value={'raw_text': 'CREATININE 90', 'page_count': 1,
                                 'structured': None}):
            resp = self.client.post(
                '/api/v1/records/upload/pdf/',
                {'pdf_file': SimpleUploadedFile('ok.pdf', payload,
                                                content_type='application/pdf')},
                format='multipart',
            )
        self.assertEqual(resp.status_code, 201)


@NO_AUTOINDEX
class UploadContentValidationTests(APITestCase):
    """Declared extension must match actual bytes on every upload route."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='upl', email='upl@example.com', password='pw-upl-1',
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        cache.clear()

    def test_html_disguised_as_pdf_is_rejected(self):
        resp = self.client.post(
            '/api/v1/records/upload/pdf/',
            {'pdf_file': SimpleUploadedFile(
                'evil.pdf', b'<html><script>window.__xss=1</script></html>',
                content_type='application/pdf')},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(MedicalRecord.objects.filter(patient=self.user).exists())

    def test_zip_disguised_as_xlsx_is_rejected_when_not_a_real_xlsx(self):
        # .xlsx is a ZIP container; a bare non-ZIP payload must not pass.
        resp = self.client.post(
            '/api/v1/records/upload/wearable/',
            {'data_file': SimpleUploadedFile('data.xlsx', b'not-a-zip-at-all',
                                             content_type='application/vnd.ms-excel')},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)

    def test_filename_traversal_is_neutralised(self):
        from apps.medical_records.services import validate_upload

        probe = SimpleUploadedFile('../../../../etc/passwd.pdf', b'%PDF-1.4 x')
        ok, safe_name = validate_upload(probe, ['pdf'])
        self.assertTrue(ok)
        self.assertNotIn('/', safe_name)
        self.assertNotIn('..', safe_name)


class OutboundRequestTests(TestCase):
    """
    There is no user-controlled outbound URL anywhere in the codebase, so there
    is no SSRF sink to validate. The one fixed external destination must still
    refuse to follow a redirect, so a hijacked or compromised host cannot bounce
    the request to an internal address.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='ssrf', email='ssrf@example.com', password='pw-ssrf-1',
        )

    def test_no_user_controlled_outbound_url_exists(self):
        """Pins the audit conclusion: outbound destinations are literals."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.rglob('*.py'):
            rel = path.relative_to(root).as_posix()
            if 'test' in rel or 'migrations/' in rel:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8-sig', errors='strict'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, 'attr', None) or getattr(func, 'id', None)
                if name not in ('get', 'post', 'put', 'patch', 'delete', 'request'):
                    continue
                mod = getattr(getattr(func, 'value', None), 'id', '')
                if 'request' not in str(mod):
                    continue
                if node.args and not isinstance(node.args[0], ast.Constant):
                    offenders.append(f'{rel}:{node.lineno}')
        self.assertEqual(offenders, [],
                         f'outbound request with a non-literal URL: {offenders}')

    def test_seizure_proxy_does_not_follow_redirects(self):
        from apps.accounts.consent import grant_consent
        from apps.accounts.models import ConsentPurpose
        from rest_framework.test import APIClient

        grant_consent(self.user, ConsentPurpose.EXTERNAL_LLM)
        api = APIClient()
        api.force_authenticate(user=self.user)

        with patch('requests.post') as spy:
            spy.return_value = MagicMock(
                status_code=200, json=lambda: {'ensemble_label': 'x'},
                raise_for_status=lambda: None,
            )
            api.post('/api/v1/seizure-analysis/',
                     {'signal_file': SimpleUploadedFile('e.parquet', b'PAR1')},
                     format='multipart')

        self.assertTrue(spy.called)
        self.assertIs(spy.call_args.kwargs.get('allow_redirects'), False,
                      'a redirect could bounce the upload to an internal address')


class CsrfProtectionTests(TestCase):
    """State-changing session-authenticated web endpoints must require CSRF."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='csrf', email='csrf@example.com', password='pw-csrf-1',
        )

    def tearDown(self):
        cache.clear()

    def test_state_changing_views_reject_a_request_without_a_token(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.user)
        for url, payload in [
            (reverse('accounts:consent'), {'purpose': 'external_llm', 'action': 'grant'}),
            (reverse('accounts:data_export'), {}),
            (reverse('accounts:profile_edit'), {'first_name': 'X'}),
            (reverse('appointments:create'), {'title': 'X'}),
        ]:
            with self.subTest(url=url):
                self.assertEqual(client.post(url, payload).status_code, 403)

    def test_login_and_registration_require_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        for url, payload in [
            (reverse('accounts:login'), {'username': 'x', 'password': 'y'}),
            (reverse('accounts:register'), {'username': 'x'}),
        ]:
            with self.subTest(url=url):
                self.assertEqual(client.post(url, payload).status_code, 403)

    def test_jwt_api_does_not_require_csrf(self):
        """JWT endpoints are not cookie-authenticated, so CSRF does not apply."""
        from rest_framework.test import APIClient

        api = APIClient(enforce_csrf_checks=True)
        api.force_authenticate(user=self.user)
        self.assertEqual(api.get('/api/v1/records/').status_code, 200)


class ApiInputValidationTests(APITestCase):
    """Malformed input must be rejected cleanly, never with a 500."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username='inp', email='inp@example.com', password='pw-inp-1',
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        cache.clear()

    def test_invalid_uuid_in_path_does_not_error(self):
        for url in ('/api/v1/records/not-a-uuid/',
                    '/api/v1/predictions/not-a-uuid/',
                    '/api/v1/assistant/sessions/not-a-uuid/'):
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (400, 404))

    def test_invalid_enum_value_is_rejected_not_stored(self):
        resp = self.client.post('/api/v1/records/upload/text/',
                                {'text': 'note', 'record_type': 'not_a_type'},
                                format='json')
        self.assertIn(resp.status_code, (201, 400))
        if resp.status_code == 201:
            rec = MedicalRecord.objects.get(patient=self.user)
            self.assertIn(rec.record_type, dict(MedicalRecord.RecordType.choices))

    def test_deeply_nested_json_is_rejected_not_crashed(self):
        payload = {'text': 'x'}
        nested = payload
        for _ in range(200):
            nested['n'] = {}
            nested = nested['n']
        resp = self.client.post('/api/v1/records/upload/text/', payload, format='json')
        self.assertIn(resp.status_code, (201, 400, 413))

    def test_unexpected_content_type_is_rejected(self):
        resp = self.client.post('/api/v1/records/upload/text/',
                                data='<xml>hi</xml>', content_type='application/xml')
        self.assertIn(resp.status_code, (400, 415))

    def test_mass_assignment_of_owner_is_ignored(self):
        other = User.objects.create_user(
            username='victim2', email='v2@example.com', password='pw-v2-1',
        )
        resp = self.client.post('/api/v1/records/upload/text/',
                                {'text': 'mine', 'patient': other.pk,
                                 'patient_id': other.pk}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(MedicalRecord.objects.filter(patient=other).exists())
        self.assertTrue(MedicalRecord.objects.filter(patient=self.user).exists())

    def test_parameter_pollution_does_not_widen_the_query(self):
        MedicalRecord.objects.create(patient=self.user, title='Mine', record_type='other')
        other = User.objects.create_user(
            username='victim3', email='v3@example.com', password='pw-v3-1',
        )
        MedicalRecord.objects.create(patient=other, title='Theirs', record_type='other')

        resp = self.client.get('/api/v1/records/?type=other&type=lab_result&q=&q=Theirs')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('Theirs', resp.content.decode())

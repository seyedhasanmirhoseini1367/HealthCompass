"""Prompt 8 of the production-readiness audit: navigation, first-run checklist,
one upload entry, legal and access-history pages, polish, SEO and i18n."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.consent import grant_consent
from apps.accounts.models import ConsentPurpose, DoctorAccessLog
from apps.medical_records.models import MedicalRecord

User = get_user_model()


def _user(name, **kw):
    return User.objects.create_user(name, email=f'{name}@test.invalid', password='pw', **kw)


class NavigationTests(TestCase):
    def test_each_role_gets_its_own_menu_with_aria_current(self):
        for role, expected, absent in (('patient', 'Records', 'My Patients'),
                                       ('doctor', 'My Patients', 'Records')):
            with self.subTest(role=role):
                user = _user(f'nav_{role}', role=role)
                self.client.force_login(user)
                body = self.client.get('/dashboard/settings/').content.decode()
                self.assertIn(f'>{expected}<', body)
                self.assertNotIn(f'>{absent}<', body)
                self.assertIn('aria-current="page"', body)

    def test_labs_link_only_when_enabled(self):
        user = _user('nav_labs')
        self.client.force_login(user)
        self.assertNotIn('/insights/models/', self.client.get('/dashboard/settings/').content.decode())
        with override_settings(LABS_ENABLED=True):
            self.assertIn('/insights/models/', self.client.get('/dashboard/settings/').content.decode())

    def test_menus_come_only_from_the_partial(self):
        from pathlib import Path

        from django.conf import settings
        for path in (Path(settings.BASE_DIR) / 'templates').rglob('*.html'):
            if 'partials' in path.parts:
                continue
            self.assertNotIn('{% for item in nav_menu %}', path.read_text(encoding='utf-8'), str(path))


class FirstRunChecklistTests(TestCase):
    def setUp(self):
        self.user = _user('fr_user')
        self.client.force_login(self.user)

    def _page(self):
        return self.client.get(reverse('dashboard:my_health')).content.decode()

    def test_shows_until_all_three_are_done(self):
        self.assertIn('Getting started', self._page())
        self.assertIn('0 of 3', self._page())
        grant_consent(self.user, ConsentPurpose.EXTERNAL_LLM)
        MedicalRecord.objects.create(patient=self.user, title='r')
        self.assertIn('2 of 3', self._page())
        from apps.rag_assistant.models import ChatSession, QueryLog
        QueryLog.objects.create(session=ChatSession.objects.create(patient=self.user, title='s'),
                                query='q', response='a', subject=self.user)
        self.assertNotIn('Getting started', self._page())

    def test_can_be_dismissed(self):
        self.client.post(reverse('dashboard:dismiss_checklist'))
        self.assertNotIn('Getting started', self._page())


class UnifiedUploadTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user('up_any')
        self.client.force_login(self.user)

    def _post(self, **data):
        return self.client.post(reverse('medical_records:upload_any'), data,
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_routes_each_type_to_its_service(self):
        with patch('apps.medical_records.views.queue_pdf',
                   return_value={'record': MedicalRecord.objects.create(patient=self.user, title='p')}) as pdf:
            self._post(file=SimpleUploadedFile('a.pdf', b'%PDF-1.4 body'))
        pdf.assert_called_once()
        with patch('apps.medical_records.views.MedicalRecordService.create_from_kanta',
                   return_value={'records_created': 1}) as kanta:
            self._post(file=SimpleUploadedFile('a.xml', b'<?xml version="1.0"?><x/>'))
        kanta.assert_called_once()
        with patch('apps.medical_records.views.MedicalRecordService.create_from_wearable',
                   return_value={'record': MedicalRecord.objects.create(patient=self.user, title='w')}) as wear:
            self._post(file=SimpleUploadedFile('a.csv', b'date,steps\n2026-01-01,100\n'))
        wear.assert_called_once()

    def test_pasted_text_and_unknown_types(self):
        response = self._post(text='Hb 140 g/L')
        self.assertTrue(response.json()['success'])
        self.assertEqual(self._post(file=SimpleUploadedFile('a.exe', b'MZ')).status_code, 400)

    def test_records_can_be_edited(self):
        record = MedicalRecord.objects.create(patient=self.user, title='Old')
        self.client.post(reverse('medical_records:edit', args=[record.pk]),
                         {'title': 'New', 'record_type': 'lab_result', 'record_date': '2026-01-02', 'notes': ''})
        record.refresh_from_db()
        self.assertEqual((record.title, str(record.record_date)), ('New', '2026-01-02'))
        other = _user('up_other')
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('medical_records:edit', args=[record.pk])).status_code, 404)


class AccessHistoryTests(TestCase):
    def test_a_patient_sees_only_their_own_rows(self):
        me, other, carer = _user('ah_me'), _user('ah_other'), _user('ah_carer')
        DoctorAccessLog.objects.create(actor=carer, patient=me, resource='record:1')
        DoctorAccessLog.objects.create(actor=carer, patient=other, resource='record:secret-of-other')
        self.client.force_login(me)
        response = self.client.get(reverse('accounts:access_history'))
        self.assertEqual(len(response.context['entries']), 1)
        self.assertEqual(response.context['entries'][0]['what'], 'Opened a record')


class LegalPageTests(TestCase):
    def test_processors_come_from_settings(self):
        with override_settings(PHI_LLM_PROVIDERS=['anthropic']):
            body = self.client.get('/privacy/').content.decode()
        self.assertIn('Anthropic', body)
        self.assertNotIn('Groq', body)

    def test_production_check_fails_while_placeholders_remain(self):
        from healthcompass.legal import check_legal_pages_are_filled_in
        self.assertEqual([e.id for e in check_legal_pages_are_filled_in(None)], ['pages.E001'])
        filled = dict(LEGAL_CONTROLLER_NAME='X Oy', LEGAL_CONTROLLER_ADDRESS='Street 1', LEGAL_BASIS='Consent',
                      CONTACT_EMAIL='p@x.invalid', LEGAL_HOSTING_PROVIDER='H', LEGAL_DATABASE_PROVIDER='D')
        with override_settings(**filled):
            self.assertEqual(check_legal_pages_are_filled_in(None), [])

    def test_contact_page_and_footer_link(self):
        self.assertEqual(self.client.get('/contact/').status_code, 200)
        self.assertIn('href="/contact/"', self.client.get('/').content.decode())


class SeoTests(TestCase):
    def test_robots_allows_public_pages_only(self):
        body = self.client.get('/robots.txt').content.decode()
        self.assertIn('Allow: /privacy/', body)
        self.assertIn('Disallow: /', body)
        self.assertIn('Sitemap:', body)

    def test_sitemap_lists_public_pages(self):
        body = self.client.get('/sitemap.xml').content.decode()
        self.assertIn('/privacy/', body)
        self.assertNotIn('/records/', body)

    def test_security_txt_and_manifest(self):
        self.assertEqual(self.client.get('/.well-known/security.txt').status_code, 200)
        self.assertEqual(self.client.get('/site.webmanifest').json()['theme_color'], '#0369a1')

    def test_private_pages_are_noindex_and_landing_is_not(self):
        self.assertNotIn('X-Robots-Tag', self.client.get('/'))
        self.assertEqual(self.client.get('/accounts/password/reset/')['X-Robots-Tag'], 'noindex')


class ErrorPageTests(TestCase):
    def test_429_has_retry_after(self):
        from django.test import RequestFactory

        from healthcompass.error_views import ratelimited
        response = ratelimited(RequestFactory().get('/'))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '60')

    def test_500_shows_a_reference(self):
        from django.test import RequestFactory

        from healthcompass.error_views import server_error
        response = server_error(RequestFactory().get('/'))
        self.assertEqual(response.status_code, 500)
        self.assertIn('Reference:', response.content.decode())

    def test_404_is_standalone(self):
        body = self.client.get('/no-such-page/').content.decode()
        self.assertIn('Page not found', body)
        self.assertNotIn('app-sidebar', body)


class I18nTests(TestCase):
    def test_html_lang_follows_the_active_language(self):
        body = self.client.get('/', HTTP_ACCEPT_LANGUAGE='fi').content.decode()
        self.assertIn('<html lang="fi">', body)

    def test_catalogues_exist(self):
        from pathlib import Path

        from django.conf import settings
        for lang in ('fi', 'sv'):
            self.assertTrue((Path(settings.BASE_DIR) / 'locale' / lang / 'LC_MESSAGES' / 'django.po').exists())

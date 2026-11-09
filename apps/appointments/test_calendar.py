"""Month view, .ics download and the Google Calendar link."""
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.appointments.calendar_export import google_url, ics
from apps.appointments.models import Appointment

User = get_user_model()


class _Base(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'cal_patient', email='cal@test.invalid', password='pw', role='patient')
        self.other = User.objects.create_user(
            'cal_other', email='cal_o@test.invalid', password='pw', role='patient')
        self.client.force_login(self.patient)

    def _appt(self, when, owner=None, **kw):
        kw.setdefault('title', 'Cardiology')
        return Appointment.objects.create(
            patient=owner or self.patient, appointment_datetime=when, **kw)


class MonthViewTests(_Base):

    def test_shows_the_requested_month_and_its_appointments(self):
        when = timezone.make_aware(datetime(2027, 3, 14, 9, 30))
        self._appt(when, title='Dentist')
        r = self.client.get('/appointments/calendar/?m=2027-03')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'March 2027')
        self.assertContains(r, 'Dentist')
        self.assertEqual(r.context['prev_m'], '2027-02')
        self.assertEqual(r.context['next_m'], '2027-04')

    def test_year_boundaries(self):
        r = self.client.get('/appointments/calendar/?m=2027-01')
        self.assertEqual(r.context['prev_m'], '2026-12')
        r = self.client.get('/appointments/calendar/?m=2026-12')
        self.assertEqual(r.context['next_m'], '2027-01')

    def test_bad_month_falls_back_to_this_month(self):
        for bad in ('nonsense', '2027-13', '2027', ''):
            r = self.client.get('/appointments/calendar/', {'m': bad})
            self.assertEqual(r.status_code, 200, bad)
            self.assertEqual(r.context['month'], timezone.localdate().replace(day=1))

    def test_other_peoples_appointments_never_appear(self):
        when = timezone.make_aware(datetime(2027, 3, 14, 9, 30))
        self._appt(when, owner=self.other, title='Someone else')
        r = self.client.get('/appointments/calendar/?m=2027-03')
        self.assertNotContains(r, 'Someone else')

    def test_requires_login(self):
        self.client.logout()
        r = self.client.get('/appointments/calendar/')
        self.assertEqual(r.status_code, 302)


class IcsTests(_Base):

    def test_download_has_event_and_chosen_alarms(self):
        appt = self._appt(timezone.now() + timedelta(days=3), location='Room 4, Main St',
                          notes='Bring results', remind_24h=True, remind_1h=True,
                          remind_3h=False, remind_2h=False)
        r = self.client.get(f'/appointments/{appt.pk}/calendar.ics')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r['Content-Type'].startswith('text/calendar'))
        self.assertIn('attachment', r['Content-Disposition'])
        body = r.content.decode()
        self.assertIn('BEGIN:VEVENT', body)
        self.assertIn('SUMMARY:Cardiology', body)
        self.assertIn('LOCATION:Room 4\\, Main St', body)
        self.assertIn('Bring results', body)
        self.assertIn('TRIGGER:-PT1440M', body)
        self.assertIn('TRIGGER:-PT60M', body)
        self.assertNotIn('TRIGGER:-PT180M', body)
        self.assertIn('\r\n', body)

    def test_someone_elses_appointment_is_404(self):
        appt = self._appt(timezone.now() + timedelta(days=3), owner=self.other)
        r = self.client.get(f'/appointments/{appt.pk}/calendar.ics')
        self.assertEqual(r.status_code, 404)

    def test_long_lines_are_folded(self):
        appt = self._appt(timezone.now(), title='Ä' * 120)
        for line in ics(appt, 'example.test').split('\r\n'):
            self.assertLessEqual(len(line.encode('utf-8')), 75)


class GoogleLinkTests(_Base):

    def test_link_has_details_but_never_notes(self):
        appt = self._appt(timezone.now() + timedelta(days=1), doctor_name='Dr Virtanen',
                          location='Main St', notes='Private health detail')
        url = google_url(appt)
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q['action'], ['TEMPLATE'])
        self.assertEqual(q['text'], ['Cardiology'])
        self.assertEqual(q['location'], ['Main St'])
        self.assertIn('Dr Virtanen', q['details'][0])
        self.assertNotIn('Private health detail', url)
        self.assertNotIn('Private', q.get('details', [''])[0])

    def test_list_page_offers_add_to_calendar(self):
        appt = self._appt(timezone.now() + timedelta(days=2))
        body = self.client.get('/appointments/').content.decode()
        self.assertIn(f'/appointments/{appt.pk}/calendar.ics', body)
        self.assertIn('calendar.google.com/calendar/render', body)
        self.assertIn('href="/appointments/calendar/"', body)

"""The Appointments page leads with the next appointment."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.appointments.models import Appointment

User = get_user_model()


class NextAppointmentTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'al_patient', email='al@test.invalid', password='pw', role='patient')
        self.client.force_login(self.patient)

    def _appt(self, title, days):
        return Appointment.objects.create(
            patient=self.patient, title=title,
            appointment_datetime=timezone.now() + timedelta(days=days))

    def test_the_soonest_appointment_leads_and_is_not_repeated(self):
        # Created out of order on purpose.
        self._appt('Dentist', 10)
        self._appt('Cardiology', 2)
        r = self.client.get('/appointments/')
        self.assertEqual(r.context['next_appt'].title, 'Cardiology')
        self.assertEqual([a.title for a in r.context['upcoming']], ['Dentist'])
        body = r.content.decode()
        self.assertIn('Next appointment', body)
        self.assertIn('Later', body)
        # Shown once as a card title (the add-to-calendar menu repeats it in
        # its label and links, which is not a duplicate card).
        self.assertEqual(body.count('class="appt-title">Cardiology<'), 1)

    def test_a_single_appointment_shows_no_later_section(self):
        self._appt('Cardiology', 2)
        body = self.client.get('/appointments/').content.decode()
        self.assertIn('Cardiology', body)
        self.assertNotIn('fa-calendar-days"></i> Later', body)

    def test_nothing_upcoming_offers_to_schedule(self):
        self._appt('Old visit', -3)
        r = self.client.get('/appointments/')
        self.assertIsNone(r.context['next_appt'])
        self.assertContains(r, 'No upcoming appointments.')

    def test_cancelled_appointment_is_never_next(self):
        a = self._appt('Cancelled one', 1)
        a.is_cancelled = True
        a.save()
        self._appt('Real one', 4)
        r = self.client.get('/appointments/')
        self.assertEqual(r.context['next_appt'].title, 'Real one')

"""
REGRESSION — NEW-12 (email case) and NEW-11 (appointments).

NEW-12 · Non-deterministic authentication
------------------------------------------
`RegisterForm.clean_email` checked `filter(email=email)` — exact — while
`EmailOrUsernameBackend` resolves with `email__iexact` and picked `.first()`
from an UNORDERED queryset. So 'Hasan@x.com' and 'hasan@x.com' could both exist
and a login by email landed in whichever row the database happened to return
first. Which account you authenticated into was decided by row order.

NEW-11 · Appointments
----------------------
Reported as three defects. Verified: the DST crash is a FALSE POSITIVE on this
stack (see AppointmentDstTests), but the other two are real — `_save_appointment`
had no length validation at all, so an over-long title reached PostgreSQL and
raised DataError -> 500; and it reset the reminder sent-flags on every save, so
editing a note re-armed reminders that had already fired and the patient was
notified twice for one appointment.
"""
from datetime import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.appointments.models import Appointment

User = get_user_model()


class EmailCaseTests(TestCase):

    def test_email_is_stored_lower_case(self):
        user = User.objects.create_user(
            username='mix', password='pw-test-only', email='Hasan@Example.COM')
        user.refresh_from_db()
        self.assertEqual(user.email, 'hasan@example.com')

    def test_registration_rejects_a_case_variant_duplicate(self):
        """ACCEPTANCE — NEW-12. 'A@x.com' after 'a@x.com' was previously allowed."""
        from apps.accounts.forms import RegisterForm

        User.objects.create_user(
            username='first', password='pw-test-only', email='dup@example.com')
        form = RegisterForm(data={
            'username': 'second', 'first_name': 'A', 'last_name': 'B',
            'email': 'DUP@example.com',
            'password1': 'sQ7!vnx2Lp0w', 'password2': 'sQ7!vnx2Lp0w'})
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)

    def test_login_by_any_casing_reaches_the_same_account(self):
        User.objects.create_user(
            username='caseuser', password='pw-test-only', email='case@example.com')
        for variant in ('case@example.com', 'CASE@EXAMPLE.COM', 'Case@Example.Com'):
            with self.subTest(email=variant):
                self.assertTrue(self.client.login(username=variant, password='pw-test-only'))
                self.client.logout()

    def test_backend_resolution_is_deterministic(self):
        """
        Ordering makes .first() stable even if a legacy duplicate survived from
        before normalisation.
        """
        import inspect
        from apps.accounts import backends
        source = inspect.getsource(backends.EmailOrUsernameBackend.authenticate)
        self.assertIn("order_by('pk')", source)

    def test_whitespace_is_stripped(self):
        user = User.objects.create_user(
            username='ws', password='pw-test-only', email='  spaced@example.com ')
        user.refresh_from_db()
        self.assertEqual(user.email, 'spaced@example.com')


class AppointmentDstTests(TestCase):
    """
    NEW-11 (DST half) — VERIFIED NOT A DEFECT on this stack.

    The audit reported that make_aware raises AmbiguousTimeError /
    NonExistentTimeError at the Europe/Helsinki transitions, crashing the app
    for two one-hour windows a year. That held under pytz. Django 5 uses
    zoneinfo, which resolves both via `fold` and raises nothing — confirmed on
    5.2.12: 2027-03-28 03:30 and 2026-10-25 03:30 both produce valid aware
    datetimes.

    These tests are kept as a guard rather than deleted: if a future Django or
    a pytz reintroduction brings those exceptions back, booking would start
    500ing and this would catch it.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='appt', password='pw-test-only', email='appt@example.com')
        self.client.force_login(self.user)

    def _post(self, dt_str, **extra):
        data = {'title': 'Check-up', 'appointment_datetime': dt_str}
        data.update(extra)
        return self.client.post(reverse('appointments:create'), data)

    def test_spring_forward_gap_does_not_crash(self):
        """zoneinfo resolves the gap; the booking succeeds rather than 500ing."""
        response = self._post('2027-03-28T03:30')
        self.assertLess(response.status_code, 500)

    def test_autumn_fallback_hour_does_not_crash(self):
        response = self._post('2026-10-25T03:30')
        self.assertLess(response.status_code, 500)

    def test_an_ordinary_time_still_saves(self):
        self._post('2026-09-15T10:30')
        self.assertTrue(Appointment.objects.filter(patient=self.user).exists())

    def test_overlong_title_is_a_form_error_not_a_500(self):
        response = self._post('2026-09-15T10:30', title='x' * 300)
        self.assertLess(response.status_code, 500)
        self.assertFalse(Appointment.objects.filter(patient=self.user).exists())

    def test_overlong_location_is_rejected(self):
        response = self._post('2026-09-15T10:30', location='x' * 600)
        self.assertLess(response.status_code, 500)
        self.assertFalse(Appointment.objects.filter(patient=self.user).exists())


class ReminderFlagTests(TestCase):
    """Editing an appointment must not re-arm reminders that already fired."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='appt2', password='pw-test-only', email='appt2@example.com')
        self.client.force_login(self.user)
        self.appt = Appointment.objects.create(
            patient=self.user, title='Original',
            appointment_datetime=timezone.make_aware(datetime(2026, 9, 15, 10, 30)),
            reminded_24h=True, reminded_1h=True)

    def test_editing_without_moving_the_time_keeps_sent_flags(self):
        """ACCEPTANCE — the patient was notified twice for one appointment."""
        self.client.post(reverse('appointments:edit', args=[self.appt.pk]),
                         {'title': 'Renamed', 'appointment_datetime': '2026-09-15T10:30'})
        self.appt.refresh_from_db()
        self.assertEqual(self.appt.title, 'Renamed')
        self.assertTrue(self.appt.reminded_24h)

    def test_moving_the_time_does_reset_sent_flags(self):
        """A rescheduled appointment genuinely needs its reminders again."""
        self.client.post(reverse('appointments:edit', args=[self.appt.pk]),
                         {'title': 'Original', 'appointment_datetime': '2026-09-20T14:00'})
        self.appt.refresh_from_db()
        self.assertFalse(self.appt.reminded_24h)

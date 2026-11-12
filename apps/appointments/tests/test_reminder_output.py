"""
The reminder sweep must be quiet when it was told to be, and when nothing happened.

A background timer runs this every ten minutes with `verbosity=0`. The command
ignored that and printed "Sent 0 appointment reminder(s)." on every run —
144 lines a day, in the dev console and in production logs, all of them meaning
"nothing happened". That is how the line that does mean something gets missed.
"""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.appointments.models import Appointment

User = get_user_model()


class ReminderOutputTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'ap_patient', email='ap@test.invalid', password='pw', role='patient')

    def _run(self, **kwargs):
        out = StringIO()
        call_command('send_appointment_reminders', stdout=out, **kwargs)
        return out.getvalue()

    def _due_appointment(self):
        """One sitting inside the 1-hour reminder window."""
        return Appointment.objects.create(
            patient=self.patient, title='Cardiology',
            appointment_datetime=timezone.now() + timedelta(hours=1),
            remind_1h=True)

    def test_a_quiet_run_with_nothing_due_prints_nothing(self):
        """ACCEPTANCE — this is the line that appeared every ten minutes."""
        self.assertEqual(self._run(verbosity=0), '')

    def test_a_normal_run_with_nothing_due_also_prints_nothing(self):
        """
        Silence is the right default too. A cron entry that emails its output
        should email nothing on the days nothing was due.
        """
        self.assertEqual(self._run(), '')

    def test_asking_for_detail_confirms_it_ran(self):
        """Someone running it by hand needs to know it is alive."""
        output = self._run(verbosity=2)

        self.assertIn('No appointment reminders were due', output)

    def test_actually_sending_is_always_announced(self):
        """The case that means something is never suppressed."""
        self._due_appointment()

        with patch('apps.notifications.firebase.send_push'):
            output = self._run()

        self.assertIn('Sent 1 appointment reminder(s).', output)

    def test_sending_is_announced_even_at_verbosity_zero(self):
        """
        Quiet means "do not narrate nothing", not "hide what you did". A
        reminder going out is a real event and the operator asked for the sweep,
        not for a vow of silence.
        """
        self._due_appointment()

        with patch('apps.notifications.firebase.send_push'):
            output = self._run(verbosity=0)

        self.assertIn('Sent 1 appointment reminder(s).', output)

    def test_the_work_still_happens_when_the_output_is_quiet(self):
        """Suppressing the message must not suppress the reminder."""
        from apps.notifications.models import Notification

        self._due_appointment()
        with patch('apps.notifications.firebase.send_push'):
            self._run(verbosity=0)

        self.assertTrue(Notification.objects.filter(user=self.patient).exists())

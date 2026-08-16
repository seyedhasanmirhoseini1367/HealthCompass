import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send push/in-app reminders for upcoming appointments'

    def handle(self, *args, **options):
        from apps.appointments.models import Appointment
        from apps.notifications.models import Notification

        now    = timezone.now()
        window = timedelta(minutes=8)

        offsets = [
            (timedelta(hours=24), 'reminded_24h', 'remind_24h', '24 hours'),
            (timedelta(hours=3),  'reminded_3h',  'remind_3h',  '3 hours'),
            (timedelta(hours=2),  'reminded_2h',  'remind_2h',  '2 hours'),
            (timedelta(hours=1),  'reminded_1h',  'remind_1h',  '1 hour'),
        ]

        sent = 0
        for offset, reminded_field, remind_field, label in offsets:
            target_start = now + offset - window
            target_end   = now + offset + window

            base_filters = {
                'appointment_datetime__gte': target_start,
                'appointment_datetime__lte': target_end,
                remind_field:    True,
                reminded_field:  False,
                'is_cancelled':  False,
            }

            # Atomic claim: SELECT FOR UPDATE SKIP LOCKED → mark sent → release.
            # If two cron workers run concurrently the second one sees 0 rows
            # because skip_locked skips any row already held by the first worker.
            with transaction.atomic():
                claimed = list(
                    Appointment.objects
                    .select_for_update(skip_locked=True)
                    .filter(**base_filters)
                    .select_related('patient')
                )
                if claimed:
                    Appointment.objects.filter(
                        pk__in=[a.pk for a in claimed]
                    ).update(**{reminded_field: True})

            for appt in claimed:
                title = f'Appointment in {label}'
                body  = appt.title
                if appt.doctor_name:
                    body += f' with {appt.doctor_name}'
                if appt.location:
                    body += f' at {appt.location}'

                # Creating the Notification is what sends the push: a post_save
                # receiver in notifications.apps does it.
                #
                # This used to ALSO call send_push() here, so every reminder
                # arrived on the patient's phone twice — once from the receiver
                # and once from this line. Two buzzes for one appointment is the
                # small end of alert fatigue, and it is the end that teaches
                # people to ignore the notification.
                Notification.objects.create(
                    user=appt.patient,
                    type='system',
                    title=title,
                    message=body,
                    link='/appointments/',
                )

                sent += 1

        # Respects the verbosity it was given, and says nothing when there was
        # nothing to say.
        #
        # This ran unconditionally. The scheduler in appointments/apps.py calls
        # it every ten minutes with verbosity=0 — explicitly asking for silence
        # — and `self.stdout.write` does not consult verbosity, so it printed
        # "Sent 0 appointment reminder(s)." forever, in the dev console and in
        # production logs alike. A line that appears 144 times a day and means
        # "nothing happened" is how the line that means something gets missed.
        verbosity = int(options.get('verbosity', 1))
        if sent:
            self.stdout.write(self.style.SUCCESS(
                f'Sent {sent} appointment reminder(s).'))
        elif verbosity >= 2:
            # Only when someone asked for detail, e.g. running it by hand to
            # check it is alive.
            self.stdout.write('No appointment reminders were due.')

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = 'Send push/in-app reminders for upcoming appointments'

    def handle(self, *args, **options):
        from apps.appointments.models import Appointment
        from apps.notifications.models import Notification
        from apps.notifications.firebase import send_push

        now = timezone.now()
        # Window: ±8 minutes around each target offset (handles cron drift)
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

            filters = {
                'appointment_datetime__gte': target_start,
                'appointment_datetime__lte': target_end,
                remind_field: True,
                reminded_field: False,
                'is_cancelled': False,
            }
            qs = Appointment.objects.filter(**filters).select_related('patient')

            for appt in qs:
                title = f'Appointment in {label}'
                body  = appt.title
                if appt.doctor_name:
                    body += f' with {appt.doctor_name}'
                if appt.location:
                    body += f' at {appt.location}'

                # In-app notification
                Notification.objects.create(
                    user=appt.patient,
                    type='system',
                    title=title,
                    message=body,
                    link='/appointments/',
                )

                # Push notification (no-op if Firebase not configured)
                try:
                    send_push(appt.patient, title, body, data={'type': 'appointment', 'id': str(appt.id)})
                except Exception:
                    pass

                # Mark as sent
                setattr(appt, reminded_field, True)
                appt.save(update_fields=[reminded_field])
                sent += 1

        self.stdout.write(self.style.SUCCESS(f'Sent {sent} appointment reminder(s).'))

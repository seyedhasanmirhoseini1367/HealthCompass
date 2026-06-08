"""
Management command: check all active care circles for missed check-ins and
send escalation notifications.

Run this every hour via cron or a task scheduler:

  # crontab -e
  0 * * * *  /path/to/venv/bin/python /path/to/manage.py check_escalations >> /var/log/care_escalations.log 2>&1

Or via Windows Task Scheduler:
  Action: python manage.py check_escalations
  Trigger: Every 1 hour
"""
import logging

from django.core.management.base import BaseCommand

from care.models import CareCircle
from care.services.escalation import check_patient_escalation, send_daily_summaries

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Check all active care circles for missed check-ins and send '
        'escalation notifications. Schedule to run every hour.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Log what would be sent without actually sending anything.',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)

        if dry_run:
            self.stdout.write(self.style.WARNING('[DRY RUN] Nothing will be sent\n'))

        circles    = CareCircle.objects.filter(is_active=True).select_related('patient')
        total_sent = 0

        for circle in circles:
            try:
                if not dry_run:
                    logs = check_patient_escalation(circle.patient)
                    if logs:
                        total_sent += len(logs)
                        self.stdout.write(
                            f'  OK {len(logs)} notification(s) sent for {circle.patient}'
                        )
                else:
                    from django.utils import timezone
                    from care.services.escalation import _hours_overdue, _tier_for_hours
                    from care.models import DailyCheckIn
                    today   = timezone.localtime(timezone.now()).date()
                    checked = DailyCheckIn.objects.filter(
                        patient=circle.patient, date=today).exists()
                    hours   = _hours_overdue(circle) if not checked else None
                    tier    = _tier_for_hours(hours) if hours is not None else None
                    status  = (
                        'checked-in OK' if checked
                        else f'{hours:.1f}h overdue, tier {tier}' if tier
                        else 'within grace window'
                    )
                    self.stdout.write(
                        f'  [dry] {circle.patient}: {status} '
                        f'(usual={circle.usual_checkin_hour}h, window={circle.checkin_window_hours}h)'
                    )
            except Exception as exc:
                logger.error('Escalation check failed for %s: %s', circle.patient, exc)
                self.stdout.write(
                    self.style.ERROR(f'  ERR {circle.patient}: {exc}')
                )

        if not dry_run:
            send_daily_summaries()

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {total_sent} escalation notification(s) across '
            f'{circles.count()} active care circle(s).'
        ))

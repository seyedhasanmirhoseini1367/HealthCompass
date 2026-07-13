"""
Management command: purge_old_query_logs

Deletes QueryLog rows older than QUERYLOG_RETENTION_DAYS (settings).
Run periodically via cron or a Railway scheduled job to enforce PHI retention.

Usage:
    python manage.py purge_old_query_logs
    python manage.py purge_old_query_logs --days 30   # override setting
    python manage.py purge_old_query_logs --dry-run   # count without deleting
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
import datetime


class Command(BaseCommand):
    help = 'Delete QueryLog rows older than QUERYLOG_RETENTION_DAYS'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='Override QUERYLOG_RETENTION_DAYS from settings',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print the count that would be deleted without deleting',
        )

    def handle(self, *args, **options):
        from apps.rag_assistant.models import QueryLog

        days = options['days'] if options['days'] is not None else getattr(settings, 'QUERYLOG_RETENTION_DAYS', 90)

        if days == 0:
            self.stdout.write('QUERYLOG_RETENTION_DAYS=0 — purging disabled.')
            return

        cutoff = timezone.now() - datetime.timedelta(days=days)
        qs     = QueryLog.objects.filter(created_at__lt=cutoff)
        count  = qs.count()

        if options['dry_run']:
            self.stdout.write(f'[dry-run] Would delete {count} QueryLog rows older than {days} days (before {cutoff.date()}).')
            return

        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {deleted} QueryLog rows older than {days} days (before {cutoff.date()}).'
        ))

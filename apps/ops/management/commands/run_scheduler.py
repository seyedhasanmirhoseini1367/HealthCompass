"""
Run scheduled jobs (audit ARCH-03). Exactly one instance should run, next to the
RQ worker (see Procfile `scheduler:`).

Every --tick seconds it enqueues the jobs that are due onto the 'default'
queue (or runs them inline when there is no worker). `--once` checks a single
time and exits, which also suits a platform cron entry.
"""
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ops.schedule import due_jobs, run_job
from healthcompass.tasks import enqueue


class Command(BaseCommand):
    help = 'Enqueue scheduled maintenance jobs when they are due.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Check once and exit.')
        parser.add_argument('--tick', type=int, default=30, help='Seconds between checks.')

    def handle(self, *args, **options):
        from apps.ops.models import JobHeartbeat

        while True:
            for name in due_jobs(timezone.now()):
                # Mark started now, so the next tick does not enqueue it again
                # while the worker is still running it.
                JobHeartbeat.objects.update_or_create(name=name,
                                                      defaults={'last_started': timezone.now()})
                self.stdout.write(f'scheduler: {name} due')
                enqueue(run_job, name, queue='default', on_commit=False)
            if options['once']:
                return
            time.sleep(options['tick'])

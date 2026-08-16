"""
One pass of the monitoring cycle. Safe to run on a schedule, safe to run twice.

    generate  materialise upcoming occurrences
    sweep     record that a grace window closed with no answer
    evaluate  apply the rules and raise signals
    dispatch  turn signals into notifications, authorised and minimised

Reports by default and acts only with --apply, the same shape as
`reconcile_orphaned_files`. Notifications go to real people's phones, so the
ability to see what a run WOULD send before it sends it is not a nicety.
"""
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run one care-monitoring cycle: generate, sweep, evaluate, dispatch.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually write occurrences, signals and notifications. '
                 'Without it, nothing is written and nobody is contacted.')
        parser.add_argument(
            '--patient', type=str, default=None,
            help='Limit to one patient (username), for investigating a single case.')
        parser.add_argument(
            '--no-dispatch', action='store_true',
            help='Do the monitoring work but contact nobody. Useful for '
                 'backfilling signals without messaging a family about history.')

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model
        from django.db import transaction

        from apps.care import scheduling, signals_rules
        from apps.notifications.dispatch import dispatch_signal

        patient = None
        if options['patient']:
            patient = get_user_model().objects.filter(
                username=options['patient']).first()
            if patient is None:
                self.stderr.write(self.style.ERROR(
                    f'No user named {options["patient"]!r}'))
                return

        apply_changes = options['apply']

        # Dry run inside a rolled-back transaction rather than by re-implementing
        # every step in "pretend" mode. A second code path that only runs during
        # dry runs is a second code path nobody tests, and it would report on
        # behaviour the real run does not have.
        if apply_changes:
            counts = self._cycle(patient, options['no_dispatch'],
                                 scheduling, signals_rules, dispatch_signal)
        else:
            try:
                with transaction.atomic():
                    counts = self._cycle(patient, True,  # never dispatch on a dry run
                                         scheduling, signals_rules, dispatch_signal)
                    raise _Rollback()
            except _Rollback:
                pass

        self.stdout.write(
            f'occurrences generated: {counts["generated"]}\n'
            f'swept to unconfirmed:  {counts["swept"]}\n'
            f'signals raised:        {counts["signals"]}\n'
            f'notifications sent:    {counts["deliveries"]}')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'Dry run — nothing was written and nobody was contacted. '
                'Re-run with --apply.'))

    @staticmethod
    def _cycle(patient, no_dispatch, scheduling, signals_rules, dispatch_signal):
        generated = scheduling.generate_occurrences(patient=patient)
        swept     = scheduling.sweep_unconfirmed(patient=patient)

        raised = (signals_rules.evaluate_patient(patient) if patient
                  else signals_rules.evaluate_all())

        deliveries = 0
        if not no_dispatch:
            for signal in raised:
                deliveries += len(dispatch_signal(signal))

        return {'generated': generated, 'swept': swept,
                'signals': len(raised), 'deliveries': deliveries}


class _Rollback(Exception):
    """Unwinds the dry-run transaction. Never escapes handle()."""

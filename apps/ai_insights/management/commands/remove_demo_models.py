"""
Remove the seeded demo AI models.

Twelve `[DEMO]` models were seeded so the catalog would not look empty before
any real model existed. They are no longer wanted, and `startup.sh` no longer
recreates them on every container start — so this only needs to run once, per
environment, to clear what previous deploys already wrote.

Deleting an AIModel cascades to its ModelPrediction rows. That matters: a
prediction is something a patient ran and can still see in their history. This
command therefore reports what it would take with it and refuses to destroy
prediction history unless explicitly told to, rather than quietly deciding that
a demo result does not count.

Usage:
    python manage.py remove_demo_models --dry-run
    python manage.py remove_demo_models
    python manage.py remove_demo_models --force   # also delete their predictions
"""
from django.core.management.base import BaseCommand

#: Seeded models were all named with this prefix, which is what makes them
#: identifiable after the fact. Matching on the name rather than a hardcoded
#: slug list means this keeps working even though the seed file is gone.
DEMO_PREFIX = '[DEMO]'


class Command(BaseCommand):
    help = 'Delete the seeded [DEMO] AI models.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be deleted; change nothing.')
        parser.add_argument('--force', action='store_true',
                            help='Delete even when patients have run predictions '
                                 'with these models (their results go too).')

    def handle(self, *args, **options):
        from apps.ai_insights.models import AIModel, ModelPrediction

        demo = AIModel.objects.filter(name__startswith=DEMO_PREFIX)
        if not demo.exists():
            self.stdout.write(self.style.SUCCESS(
                'Nothing to do — no demo models in this database.'))
            return

        predictions = ModelPrediction.objects.filter(model__in=demo)
        prediction_count = predictions.count()

        self.stdout.write(f'{demo.count()} demo model(s):')
        for model in demo.order_by('name'):
            own = model.predictions.count()
            suffix = f'  [{own} prediction(s)]' if own else ''
            self.stdout.write(f'  {model.slug:<40} {model.name}{suffix}')

        if options['dry_run']:
            self.stdout.write(self.style.WARNING(
                f'--dry-run: nothing was changed. '
                f'{prediction_count} prediction(s) would be deleted with them.'))
            return

        if prediction_count and not options['force']:
            self.stderr.write(self.style.ERROR(
                f'Refusing to delete: {prediction_count} prediction(s) were made '
                f'with these models, and deleting the models deletes those '
                f'results from the patients\' history too.\n'
                f'Re-run with --force if that is intended.'))
            return

        deleted, by_model = demo.delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted {deleted} row(s).'))
        for label, count in sorted(by_model.items()):
            self.stdout.write(f'  {label}: {count}')

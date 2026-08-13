"""
Remove the twelve seeded demo AI models.

They were placeholders so the catalog would not look empty before any real model
existed. `seed_demo_models` created them on every container start; that command
and the startup hook are gone, so this only needs to run once per environment to
clear what earlier deploys already wrote.

Why this is not a one-line delete
---------------------------------
`ModelPrediction.model` is CASCADE. Deleting an AIModel therefore destroys the
prediction history of every patient who ran it, and — because Django does not
touch storage on cascade — leaves each prediction's `input_file` behind as an
unreachable blob: the row that named it is gone, so `_user_can_access_media`
can no longer attribute it to anyone and nothing in the application can find it
again.

So the paths are collected before the delete, and removed after it commits,
mirroring the ordering in `apps.accounts.services.purge_user_data`: file
deletion has no compensating action, so it must not happen inside a transaction
that could still roll back. A file that cannot be removed is logged at ERROR
with the prediction it belonged to, because a silently orphaned blob of patient
input is exactly the thing nobody discovers later.

Safety
------
Matching is by slug, but a slug is not proof of identity — a real submitted
model could hold one of these slugs. Every matched row must also be named
`[DEMO]`; one that is not aborts the whole run rather than being skipped, since
a collision means the assumption behind this command is wrong.

Usage:
    python manage.py remove_demo_models              # dry run — reports only
    python manage.py remove_demo_models --confirm    # actually deletes
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

logger = logging.getLogger(__name__)

#: The twelve slugs written by the deleted `seed_demo_models` command, copied
#: from it verbatim before it was removed. Hardcoded rather than matched on the
#: name prefix so this command can only ever touch these exact rows.
DEMO_SLUGS = (
    'diabetes-risk-predictor',
    'cardiovascular-risk-score',
    'ckd-stage-classifier',
    'chest-xray-pathology-detector',
    'diabetic-retinopathy-grader',
    'brain-mri-tumour-segmentation',
    'eeg-seizure-detector',
    'ecg-arrhythmia-classifier',
    'ppg-stress-estimator',
    'sleep-quality-analyser',
    'wearable-activity-recovery',
    'breast-cancer-risk-stratifier',
)

#: Every matched row must carry this. See "Safety" above.
DEMO_NAME_PREFIX = '[DEMO]'


class Command(BaseCommand):
    help = 'Delete the twelve seeded [DEMO] AI models (dry run unless --confirm).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm', action='store_true',
            help='Actually delete. Without this the command only reports.')

    def handle(self, *args, **options):
        from apps.ai_insights.models import AIModel

        models = list(AIModel.objects.filter(slug__in=DEMO_SLUGS)
                      .order_by('slug').prefetch_related('predictions'))
        found = {model.slug: model for model in models}

        # ── Refuse on any row that is not what we think it is ────────────────
        impostors = [m for m in models if not m.name.startswith(DEMO_NAME_PREFIX)]
        if impostors:
            listed = '\n'.join(f'  {m.slug}  →  {m.name!r}' for m in impostors)
            raise CommandError(
                f'Aborting: {len(impostors)} row(s) hold a demo slug but are not '
                f'named {DEMO_NAME_PREFIX}:\n{listed}\n'
                f'A real submitted model has taken one of these slugs. Nothing '
                f'was deleted — rename or re-slug it first, then re-run.'
            )

        # ── Report ───────────────────────────────────────────────────────────
        total_predictions = 0
        total_files = 0

        self.stdout.write(f'{len(DEMO_SLUGS)} demo slug(s):')
        for slug in DEMO_SLUGS:
            model = found.get(slug)
            if model is None:
                self.stdout.write(f'  {slug:<34} absent')
                continue

            predictions = list(model.predictions.all())
            with_files = [p for p in predictions if p.input_file]
            total_predictions += len(predictions)
            total_files += len(with_files)

            self.stdout.write(
                f'  {slug:<34} present · {len(predictions)} prediction(s) · '
                f'{len(with_files)} with an input file'
            )

        if not models:
            self.stdout.write(self.style.SUCCESS(
                'Nothing to do — none of these exist in this database.'))
            return

        self.stdout.write('')
        self.stdout.write(
            f'Deleting would remove {len(models)} model(s), '
            f'{total_predictions} prediction(s) and {total_files} input file(s).'
        )

        if not options['confirm']:
            self.stdout.write(self.style.WARNING(
                'Dry run — nothing was changed. Re-run with --confirm to delete.'))
            return

        # ── Delete ───────────────────────────────────────────────────────────
        removed_models = 0
        for model in models:
            # Collected BEFORE the delete: afterwards the rows naming these
            # files are gone and there is no way to find them.
            doomed = [(prediction.pk, prediction.input_file)
                      for prediction in model.predictions.all()
                      if prediction.input_file]

            with transaction.atomic():
                slug = model.slug
                model.delete()
                # Deliberately after the commit. If the transaction rolled back,
                # the rows would return while the bytes were already gone
                # irreversibly; an orphaned file is recoverable, a row pointing
                # at nothing is not.
                transaction.on_commit(
                    lambda files=doomed, s=slug: self._remove_files(files, s))

            removed_models += 1
            self.stdout.write(f'  deleted {slug}')

        self.stdout.write(self.style.SUCCESS(
            f'Removed {removed_models} demo model(s).'))

    def _remove_files(self, files, slug):
        """Delete input files whose prediction rows have just been removed."""
        failed = 0
        for prediction_pk, field_file in files:
            try:
                field_file.delete(save=False)
            except Exception as exc:
                failed += 1
                logger.error(
                    'remove_demo_models: could not delete input file %s for '
                    'prediction %s (model %s): %s',
                    field_file.name, prediction_pk, slug, exc)

        if failed:
            self.stderr.write(self.style.ERROR(
                f'  {failed} input file(s) from {slug} could not be removed and '
                f'are now orphaned on storage — see the log for each one.'))
        elif files:
            self.stdout.write(f'  removed {len(files)} input file(s) from {slug}')

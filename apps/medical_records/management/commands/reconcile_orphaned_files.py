"""
Find uploaded files that no database row refers to.

Why this exists
---------------
File erasure is scheduled with `transaction.on_commit`, so the bytes are removed
only after the deletion is durable. That is deliberate — deleting inside the
transaction would destroy the file for a row that a rollback brings back — but
it leaves two windows that nothing else can close:

  * storage deletion fails after the commit (permissions, a full disk, an object
    store outage). An ERASURE_INCOMPLETE event is emitted, but the row naming
    the file is already gone, so nothing in the database can find it again.
  * the process dies between the commit and the callback. Then there is not even
    an event: the row is gone, the file remains, and no trace was written.

Neither is hypothetical on a container platform that restarts on deploy. This
command is the reconciliation those windows require: it is the only way to
answer "which files are orphaned?" after the fact.

Scope
-----
Every model that stores an uploaded file:

    MedicalRecord.file            medical_records/
    ModelPrediction.input_file    prediction_inputs/
    AIModel.model_file            ai_models/
    CustomUser.profile_picture    profile_pics/

Safety
------
Reports by default and deletes only with --delete, because the failure mode of
being wrong here is destroying a file that IS referenced.

Files newer than --min-age-hours are never considered: an upload in flight has
bytes on storage before its row is committed, and treating that as an orphan
would delete a record the patient is in the middle of creating.

Comparison is by exact stored name against the database, never by pattern or
directory heuristic, so a file that any row still references cannot be selected.

Usage:
    python manage.py reconcile_orphaned_files
    python manage.py reconcile_orphaned_files --min-age-hours 48
    python manage.py reconcile_orphaned_files --delete
"""
from django.core.management.base import BaseCommand

#: (label, dotted model, field name, storage prefix)
FILE_FIELDS = (
    ('medical record',    'medical_records.MedicalRecord', 'file',            'medical_records/'),
    ('prediction input',  'ai_insights.ModelPrediction',   'input_file',      'prediction_inputs/'),
    ('model artifact',    'ai_insights.AIModel',           'model_file',      'ai_models/'),
    ('profile picture',   'accounts.CustomUser',           'profile_picture', 'profile_pics/'),
)


class Command(BaseCommand):
    help = 'Report (or delete) uploaded files that no database row references.'

    def add_arguments(self, parser):
        parser.add_argument('--delete', action='store_true',
                            help='Remove the orphans. Without this, only reports.')
        parser.add_argument('--min-age-hours', type=int, default=24,
                            help='Ignore files younger than this (default 24), so an '
                                 'upload in flight is never mistaken for an orphan.')

    def handle(self, *args, **options):
        import datetime

        from django.apps import apps as django_apps
        from django.core.files.storage import default_storage
        from django.utils import timezone

        cutoff = timezone.now() - datetime.timedelta(hours=options['min_age_hours'])
        total_orphans = 0
        total_deleted = 0
        total_failed = 0

        for label, dotted, field, prefix in FILE_FIELDS:
            model = django_apps.get_model(dotted)

            # Every name this field currently points at. Compared exactly; a file
            # referenced by any row is never a candidate.
            referenced = set(
                model.objects.exclude(**{f'{field}': ''})
                             .exclude(**{f'{field}__isnull': True})
                             .values_list(field, flat=True)
            )

            try:
                stored = self._walk(default_storage, prefix)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(
                    f'{label}: could not list {prefix}: {exc}'))
                continue

            orphans = []
            for name in stored:
                if name in referenced:
                    continue
                try:
                    modified = default_storage.get_modified_time(name)
                    if timezone.is_naive(modified):
                        modified = timezone.make_aware(modified)
                    if modified > cutoff:
                        continue        # too new to judge
                except Exception:
                    continue            # cannot age it — leave it alone
                orphans.append(name)

            total_orphans += len(orphans)
            if not orphans:
                self.stdout.write(f'{label:<18} no orphans '
                                  f'({len(referenced)} referenced)')
                continue

            self.stdout.write(self.style.WARNING(
                f'{label:<18} {len(orphans)} orphan(s) of {len(stored)} file(s)'))
            for name in orphans[:10]:
                self.stdout.write(f'    {name}')
            if len(orphans) > 10:
                self.stdout.write(f'    … and {len(orphans) - 10} more')

            if not options['delete']:
                continue

            from apps.accounts.services import erase_uploaded_file
            for name in orphans:
                if erase_uploaded_file(default_storage, name, label=f'orphan:{label}'):
                    total_deleted += 1
                else:
                    total_failed += 1

        self.stdout.write('')
        if not total_orphans:
            self.stdout.write(self.style.SUCCESS(
                'Storage and database agree — no orphaned files.'))
            return

        if options['delete']:
            self.stdout.write(self.style.SUCCESS(f'Deleted {total_deleted} orphan(s).'))
            if total_failed:
                self.stdout.write(self.style.ERROR(
                    f'{total_failed} could not be deleted — see the log.'))
        else:
            self.stdout.write(self.style.WARNING(
                f'{total_orphans} orphan(s) found. Nothing was changed; '
                f're-run with --delete to remove them.'))

    @staticmethod
    def _walk(storage, prefix):
        """Every stored name under *prefix*, recursively."""
        found = []
        pending = [prefix.rstrip('/')]
        while pending:
            current = pending.pop()
            try:
                dirs, files = storage.listdir(current)
            except (FileNotFoundError, NotADirectoryError, OSError):
                continue
            for name in files:
                found.append(f'{current}/{name}' if current else name)
            for name in dirs:
                pending.append(f'{current}/{name}' if current else name)
        return found

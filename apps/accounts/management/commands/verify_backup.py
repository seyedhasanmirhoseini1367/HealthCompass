"""
Management command: prove a backup actually restores.

A backup that has never been restored is not a backup. This restores a dump into
a SCRATCH database and checks the result against the manifest captured at backup
time — row counts per model, applied migrations, and patient-ownership
invariants.

Safety rails, deliberately strict:

  * It refuses to restore into the `default` alias. A verification run must never
    be able to become a destructive production operation by mistyping a flag.
  * It never writes to the source database.
  * It exits non-zero when the restore is unfaithful, so it can gate a release.

Usage:
    python manage.py verify_backup --backup backups/x.sqlite3 \\
                                   --manifest backups/x.manifest.json
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Restore a backup into a scratch database and verify it is faithful.'

    def add_arguments(self, parser):
        parser.add_argument('--backup', required=True, help='Path to the dump file.')
        parser.add_argument('--manifest', default=None,
                            help='Manifest JSON (defaults to <backup>.manifest.json).')
        parser.add_argument('--scratch-alias', default='verify_scratch',
                            help='Database alias to restore into. Must not be "default".')
        parser.add_argument('--keep', action='store_true',
                            help='Keep the restored scratch database for inspection.')

    def handle(self, *args, **options):
        import shutil
        import tempfile

        from django.conf import settings
        from django.db import connections

        from healthcompass.backup import (
            compare_manifests, database_engine, file_checksum,
            integrity_checks, row_counts, migration_head,
        )

        alias = options['scratch_alias']
        if alias == 'default':
            raise CommandError(
                'Refusing to restore into the "default" alias. Verification must '
                'target a scratch database; restoring over the live one is how a '
                'backup test becomes an outage.'
            )

        backup = Path(options['backup'])
        if not backup.is_file():
            raise CommandError(f'Backup not found: {backup}')

        manifest_path = Path(options['manifest']) if options['manifest'] \
            else backup.with_suffix('.manifest.json')
        if not manifest_path.is_file():
            raise CommandError(f'Manifest not found: {manifest_path}')

        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

        # 1. The dump must be the one the manifest describes.
        recorded = manifest.get('dump_sha256')
        if recorded:
            actual = file_checksum(backup)
            if actual != recorded:
                raise CommandError(
                    f'Checksum mismatch — the dump does not match its manifest.\n'
                    f'  manifest: {recorded}\n  actual:   {actual}')
            self.stdout.write('  checksum : OK')

        engine = manifest.get('engine')
        if engine != 'sqlite':
            # Postgres verification needs pg_restore and a scratch server, which
            # is an infrastructure capability rather than a repository one.
            raise CommandError(
                f'Automated restore verification is implemented for sqlite dumps. '
                f'This manifest records engine={engine!r}; verify it with '
                f'pg_restore into a scratch database per docs/BACKUP_RESTORE.md, '
                f'and do not record the backup as verified until that has run.'
            )

        # 2. Restore into a scratch copy — never the source file.
        scratch_dir = Path(tempfile.mkdtemp(prefix='hc-verify-'))
        scratch_db  = scratch_dir / 'restored.sqlite3'
        shutil.copy2(backup, scratch_db)

        settings.DATABASES[alias] = {
            **settings.DATABASES['default'],
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME':   str(scratch_db),
        }
        connections.databases[alias] = settings.DATABASES[alias]

        try:
            restored = {
                'row_counts': row_counts(alias),
                'migrations': migration_head(alias),
            }
            problems = compare_manifests(manifest, restored)

            self.stdout.write(f'  restored : {scratch_db}')
            self.stdout.write(f'  models   : {len(restored["row_counts"])}')

            checks = integrity_checks(alias)
            for c in checks:
                mark = 'OK  ' if c['ok'] else 'FAIL'
                self.stdout.write(f'  {mark} {c["check"]} '
                                  f'({c["violations"]} violation(s))')
            failed_checks = [c for c in checks if not c['ok']]

            if problems or failed_checks:
                for p in problems:
                    self.stdout.write(self.style.ERROR(f'  MISMATCH {p}'))
                raise CommandError(
                    f'Restore verification FAILED: {len(problems)} count/migration '
                    f'mismatch(es), {len(failed_checks)} integrity violation(s). '
                    f'This backup must not be relied on.')

            self.stdout.write(self.style.SUCCESS(
                'Restore verified: row counts, migrations and ownership '
                'invariants all match the manifest.'))
        finally:
            connections[alias].close()
            connections.databases.pop(alias, None)
            settings.DATABASES.pop(alias, None)
            if not options['keep']:
                shutil.rmtree(scratch_dir, ignore_errors=True)

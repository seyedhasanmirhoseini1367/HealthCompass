"""
Management command: take a database backup with a verifiable manifest.

The repository previously had no backup mechanism at all. This produces two
files:

    <out>/healthcompass-<engine>-<timestamp>.(sqlite3|dump)
    <out>/healthcompass-<engine>-<timestamp>.manifest.json

The manifest is the point. It records row counts for every model, the applied
migration set, and a SHA-256 of the dump, so `verify_backup` can prove a restore
is faithful instead of merely non-erroring.

Usage:
    python manage.py backup_database
    python manage.py backup_database --output /var/backups/healthcompass
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Back up the database and write a verifiable manifest alongside it.'

    def add_arguments(self, parser):
        parser.add_argument('--output', default='backups',
                            help='Directory to write the dump and manifest into.')
        parser.add_argument('--alias', default='default',
                            help='Database alias to back up.')

    def handle(self, *args, **options):
        from healthcompass.backup import (
            build_manifest, database_engine, dump_postgres, dump_sqlite,
            file_checksum,
        )

        alias  = options['alias']
        engine = database_engine(alias)
        outdir = Path(options['output'])
        stamp  = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

        # Built BEFORE the dump so the recorded counts describe the state the
        # dump is taken from, not a later one.
        manifest = build_manifest(alias)

        if engine == 'sqlite':
            target = outdir / f'healthcompass-sqlite-{stamp}.sqlite3'
            dump_sqlite(alias, target)
        elif engine == 'postgresql':
            target = outdir / f'healthcompass-postgres-{stamp}.dump'
            try:
                dump_postgres(alias, target)
            except Exception as exc:
                raise CommandError(f'pg_dump failed: {exc}')
        else:
            raise CommandError(
                f'Unsupported database engine {engine!r}. Refusing to guess a '
                f'backup strategy for an engine this command has not been '
                f'verified against.'
            )

        manifest['dump_file']     = target.name
        manifest['dump_bytes']    = target.stat().st_size
        manifest['dump_sha256']   = file_checksum(target)

        manifest_path = target.with_suffix('.manifest.json')
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')

        total_rows = sum(v for v in manifest['row_counts'].values() if v > 0)
        self.stdout.write(self.style.SUCCESS(f'Backup written: {target}'))
        self.stdout.write(f'  manifest : {manifest_path}')
        self.stdout.write(f'  engine   : {engine}')
        self.stdout.write(f'  rows     : {total_rows} across '
                          f'{len(manifest["row_counts"])} models')
        self.stdout.write(f'  sha256   : {manifest["dump_sha256"][:16]}…')
        self.stdout.write(self.style.WARNING(
            'A backup is not verified until it has been restored. '
            'Run: manage.py verify_backup --backup <dump> --manifest <manifest>'))

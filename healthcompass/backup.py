"""
Backup, restore-verification and data-integrity primitives.

The project had no backup mechanism of any kind — the only mentions of the word
anywhere in the repository were in the audit documents describing its absence.
For a system holding medical records that is a durability gap, not a convenience
gap.

Design notes:

* A backup that has never been restored is not a backup. Everything here is
  built around producing a MANIFEST alongside the dump, so a restore can be
  checked against what was actually captured rather than merely "not erroring".

* The integrity checks are ownership invariants, not row counts alone. A restore
  that returns the right number of rows but attaches a chunk to the wrong
  patient is worse than a failed restore, because it looks successful.

* Nothing here writes to the source database. `verify_restore` refuses to run
  against the configured default alias precisely so a verification run can never
  become a destructive production operation.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from django.apps import apps as django_apps
from django.conf import settings
from django.db import connections

MANIFEST_VERSION = '1.0'


# ── Engine detection ──────────────────────────────────────────────────────────

def database_engine(alias: str = 'default') -> str:
    """'postgresql' | 'sqlite' | the raw ENGINE string when neither."""
    engine = settings.DATABASES[alias].get('ENGINE', '')
    if 'postgresql' in engine:
        return 'postgresql'
    if 'sqlite' in engine:
        return 'sqlite'
    return engine


def pg_dump_available() -> bool:
    return shutil.which('pg_dump') is not None


# ── Manifest ──────────────────────────────────────────────────────────────────

def row_counts(alias: str = 'default') -> Dict[str, int]:
    """
    Row count for every concrete model, keyed 'app_label.ModelName'.

    Counting every model rather than a hand-picked list means a future model
    cannot silently fall outside backup verification.
    """
    counts: Dict[str, int] = {}
    for model in django_apps.get_models():
        if model._meta.abstract or model._meta.proxy:
            continue
        label = f'{model._meta.app_label}.{model.__name__}'
        try:
            counts[label] = model.objects.using(alias).count()
        except Exception:                       # table absent in this database
            counts[label] = -1
    return counts


def migration_head(alias: str = 'default') -> List[str]:
    """Applied migrations, so a restore can be checked for schema drift."""
    from django.db.migrations.recorder import MigrationRecorder
    recorder = MigrationRecorder(connections[alias])
    try:
        return sorted(f'{app}.{name}' for app, name in recorder.applied_migrations())
    except Exception:
        return []


def build_manifest(alias: str = 'default') -> Dict[str, Any]:
    return {
        'manifest_version': MANIFEST_VERSION,
        'created_at':       datetime.now(timezone.utc).isoformat(),
        'engine':           database_engine(alias),
        'row_counts':       row_counts(alias),
        'migrations':       migration_head(alias),
    }


def file_checksum(path: Path) -> str:
    """SHA-256 of the dump, so corruption in transit is detectable."""
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


# ── Integrity invariants ──────────────────────────────────────────────────────

def integrity_checks(alias: str = 'default') -> List[Dict[str, Any]]:
    """
    Ownership and referential invariants that must hold in any usable restore.

    These are deliberately about patient boundaries rather than row counts: a
    restore with correct totals but a chunk attached to the wrong patient is a
    silent isolation breach, and would otherwise look like success.
    """
    from apps.medical_records.models import ParsedLabValue, WearableDataPoint
    from apps.rag_assistant.models import MedicalChunk, MedicalDocument

    results: List[Dict[str, Any]] = []

    def check(name: str, count: int, detail: str):
        results.append({'check': name, 'violations': count, 'ok': count == 0,
                        'detail': detail})

    # A chunk must belong to the same patient as the document it came from.
    check(
        'chunk_patient_matches_document',
        MedicalChunk.objects.using(alias)
            .exclude(document__patient_id=None)
            .exclude(patient_id=models_f('document__patient_id')).count(),
        'MedicalChunk.patient must equal MedicalChunk.document.patient',
    )

    # A document must belong to the same patient as the record it came from.
    check(
        'document_patient_matches_record',
        MedicalDocument.objects.using(alias)
            .exclude(record__isnull=True)
            .exclude(patient_id=models_f('record__patient_id')).count(),
        'MedicalDocument.patient must equal MedicalDocument.record.patient',
    )

    # Lab values and wearable points reach their owner only through the record.
    check(
        'lab_values_have_a_record',
        ParsedLabValue.objects.using(alias).filter(record__isnull=True).count(),
        'ParsedLabValue.record must not be null (ownership is derived from it)',
    )
    check(
        'wearable_points_have_a_record',
        WearableDataPoint.objects.using(alias).filter(record__isnull=True).count(),
        'WearableDataPoint.record must not be null',
    )

    return results


def models_f(field: str):
    """Local alias so the queries above read as invariants rather than ORM noise."""
    from django.db.models import F
    return F(field)


def compare_manifests(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """
    Differences that matter between the captured and the restored database.

    Returns an empty list when the restore is faithful.
    """
    problems: List[str] = []

    b_counts, a_counts = before.get('row_counts', {}), after.get('row_counts', {})
    for label, expected in sorted(b_counts.items()):
        actual = a_counts.get(label)
        if actual is None:
            problems.append(f'{label}: missing from restored database')
        elif actual != expected:
            problems.append(f'{label}: expected {expected} rows, restored {actual}')

    missing_migrations = set(before.get('migrations', [])) - set(after.get('migrations', []))
    if missing_migrations:
        problems.append(f'restored database is missing migrations: '
                        f'{sorted(missing_migrations)[:5]}')

    return problems


# ── Dump / restore ────────────────────────────────────────────────────────────

def dump_sqlite(alias: str, destination: Path) -> Path:
    """
    Consistent snapshot via sqlite3's own backup API.

    A plain file copy of a live SQLite database can capture a torn write; the
    backup API takes a consistent snapshot instead, and needs no external binary.
    """
    import sqlite3

    source_path = settings.DATABASES[alias]['NAME']
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f'file:{source_path}?mode=ro', uri=True)
    try:
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return destination


def dump_postgres(alias: str, destination: Path) -> Path:
    """
    pg_dump in custom format (-Fc), which restores with pg_restore and supports
    parallelism and selective restore.

    Raises when pg_dump is unavailable rather than falling back to a weaker
    mechanism — a backup silently taken by a different method is how an
    unverifiable backup happens.
    """
    if not pg_dump_available():
        raise RuntimeError(
            'pg_dump is not on PATH. Install the PostgreSQL client tools in the '
            'environment that runs backups. Refusing to substitute a weaker '
            'dump mechanism silently.'
        )
    cfg = settings.DATABASES[alias]
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'pg_dump', '--format=custom', '--no-owner', '--no-privileges',
        '--file', str(destination),
        '--host', cfg.get('HOST') or 'localhost',
        '--port', str(cfg.get('PORT') or 5432),
        '--username', cfg.get('USER') or '',
        cfg.get('NAME') or '',
    ]
    env_password = cfg.get('PASSWORD') or ''
    import os
    env = {**os.environ, 'PGPASSWORD': env_password} if env_password else None
    subprocess.run(cmd, check=True, env=env, capture_output=True)
    return destination

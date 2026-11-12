"""
Tests — Phase 1: backup and restore verification.

The repository had no backup mechanism at all; the only occurrences of the word
were in audit documents describing its absence. For a medical record system that
is a durability gap.

The property under test is not "a dump file was produced" but "a restore can be
proven faithful". These tests therefore concentrate on the manifest comparison
and the ownership invariants — a restore with correct row totals that attaches a
chunk to the wrong patient is worse than a failed restore, because it looks like
success.

The command-level safety rails are tested too: verification must be incapable of
writing over the live database, however it is invoked.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.rag_assistant.models import MedicalChunk, MedicalDocument
from healthcompass.backup import (
    build_manifest, compare_manifests, file_checksum, integrity_checks,
    row_counts,
)


class ManifestTests(TestCase):

    def test_manifest_counts_every_concrete_model(self):
        """
        Counting all models rather than a curated list means a future model
        cannot silently fall outside backup verification.
        """
        manifest = build_manifest()
        self.assertIn('accounts.CustomUser', manifest['row_counts'])
        self.assertIn('medical_records.MedicalRecord', manifest['row_counts'])
        self.assertIn('rag_assistant.MedicalChunk', manifest['row_counts'])
        self.assertGreater(len(manifest['row_counts']), 20)

    def test_manifest_records_engine_and_migrations(self):
        manifest = build_manifest()
        self.assertIn(manifest['engine'], ('sqlite', 'postgresql'))
        self.assertIsInstance(manifest['migrations'], list)

    def test_row_counts_track_reality(self):
        before = row_counts()['accounts.CustomUser']
        get_user_model().objects.create_user(
            username='backup-counted', password='pw-test-only',
            email='bc@example.com')
        self.assertEqual(row_counts()['accounts.CustomUser'], before + 1)


class ManifestComparisonTests(TestCase):
    """The check that turns a dump into a *verified* backup."""

    def _manifest(self, counts, migrations=None):
        return {'row_counts': counts, 'migrations': migrations or []}

    def test_identical_manifests_have_no_problems(self):
        m = self._manifest({'a.B': 5, 'c.D': 0})
        self.assertEqual(compare_manifests(m, m), [])

    def test_missing_rows_are_reported(self):
        before = self._manifest({'medical_records.MedicalRecord': 744})
        after = self._manifest({'medical_records.MedicalRecord': 700})
        problems = compare_manifests(before, after)
        self.assertEqual(len(problems), 1)
        self.assertIn('expected 744 rows, restored 700', problems[0])

    def test_missing_model_is_reported(self):
        problems = compare_manifests(self._manifest({'a.B': 1}), self._manifest({}))
        self.assertIn('missing from restored database', problems[0])

    def test_missing_migrations_are_reported(self):
        before = self._manifest({}, ['rag_assistant.0010_x'])
        after = self._manifest({}, [])
        self.assertIn('missing migrations', compare_manifests(before, after)[0])

    def test_extra_rows_are_also_a_mismatch(self):
        """A restore with MORE rows is not faithful either."""
        problems = compare_manifests(self._manifest({'a.B': 1}), self._manifest({'a.B': 2}))
        self.assertEqual(len(problems), 1)


class ChecksumTests(TestCase):

    def test_checksum_detects_a_single_changed_byte(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'dump.bin'
            p.write_bytes(b'medical-record-dump')
            original = file_checksum(p)
            p.write_bytes(b'medical-record-dumq')
            self.assertNotEqual(original, file_checksum(p))


class IntegrityInvariantTests(TestCase):
    """
    Ownership invariants. These are the checks that distinguish a usable restore
    from one that silently crosses patient boundaries.
    """

    def setUp(self):
        self.a = get_user_model().objects.create_user(
            username='inv-a', password='pw-test-only', email='a@example.com')
        self.b = get_user_model().objects.create_user(
            username='inv-b', password='pw-test-only', email='b@example.com')

    def test_clean_database_passes_every_check(self):
        for c in integrity_checks():
            self.assertTrue(c['ok'], f'{c["check"]} reported {c["violations"]}')

    def test_chunk_attached_to_the_wrong_patient_is_detected(self):
        """
        Nothing in the schema prevents this today, which is exactly why the
        restore check has to look for it.
        """
        doc = MedicalDocument.objects.create(
            patient=self.a, title='A doc', document_type='lab_result', content='c')
        MedicalChunk.objects.create(
            document=doc, patient=self.b, chunk_index=0, content='leaked')

        failed = [c for c in integrity_checks() if not c['ok']]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['check'], 'chunk_patient_matches_document')
        self.assertEqual(failed[0]['violations'], 1)

    def test_document_attached_to_the_wrong_patient_is_detected(self):
        from apps.medical_records.models import MedicalRecord
        record = MedicalRecord.objects.create(
            patient=self.a, title='rec', record_type='lab_result')
        MedicalDocument.objects.create(
            patient=self.b, record=record, title='D',
            document_type='lab_result', content='c')

        failed = [c for c in integrity_checks() if not c['ok']]
        self.assertTrue(any(c['check'] == 'document_patient_matches_record'
                            for c in failed))


class VerifyCommandSafetyTests(TestCase):
    """A verification run must never be able to become a production incident."""

    def test_refuses_to_restore_into_the_default_alias(self):
        with tempfile.TemporaryDirectory() as d:
            dump = Path(d) / 'x.sqlite3'
            dump.write_bytes(b'not-a-real-dump')
            with self.assertRaises(CommandError) as ctx:
                call_command('verify_backup', backup=str(dump),
                             scratch_alias='default', verbosity=0)
            self.assertIn('Refusing to restore into the "default" alias',
                          str(ctx.exception))

    def test_missing_backup_file_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command('verify_backup', backup='/nonexistent/x.sqlite3', verbosity=0)

    def test_missing_manifest_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            dump = Path(d) / 'x.sqlite3'
            dump.write_bytes(b'dump')
            with self.assertRaises(CommandError) as ctx:
                call_command('verify_backup', backup=str(dump), verbosity=0)
            self.assertIn('Manifest not found', str(ctx.exception))

    def test_checksum_mismatch_aborts_verification(self):
        """A dump that does not match its manifest must never be trusted."""
        with tempfile.TemporaryDirectory() as d:
            dump = Path(d) / 'x.sqlite3'
            dump.write_bytes(b'original-bytes')
            manifest = dump.with_suffix('.manifest.json')
            manifest.write_text(json.dumps({
                'engine': 'sqlite', 'row_counts': {}, 'migrations': [],
                'dump_sha256': 'deadbeef' * 8,
            }), encoding='utf-8')
            with self.assertRaises(CommandError) as ctx:
                call_command('verify_backup', backup=str(dump),
                             manifest=str(manifest), verbosity=0)
            self.assertIn('Checksum mismatch', str(ctx.exception))

    def test_postgres_dumps_are_not_reported_as_verified(self):
        """
        Automated restore verification is implemented for sqlite. A postgres
        manifest must produce an explicit "not verified here" error rather than
        quietly passing.
        """
        with tempfile.TemporaryDirectory() as d:
            dump = Path(d) / 'x.dump'
            dump.write_bytes(b'pgdump')
            manifest = dump.with_suffix('.manifest.json')
            manifest.write_text(json.dumps({
                'engine': 'postgresql', 'row_counts': {}, 'migrations': [],
            }), encoding='utf-8')
            with self.assertRaises(CommandError) as ctx:
                call_command('verify_backup', backup=str(dump),
                             manifest=str(manifest), verbosity=0)
            self.assertIn('pg_restore', str(ctx.exception))


class BackupCommandTests(TestCase):

    def test_unsupported_engine_refuses_rather_than_guessing(self):
        with patch('healthcompass.backup.database_engine', return_value='oracle'):
            with self.assertRaises(CommandError) as ctx:
                call_command('backup_database', output=tempfile.mkdtemp(), verbosity=0)
            self.assertIn('Unsupported database engine', str(ctx.exception))

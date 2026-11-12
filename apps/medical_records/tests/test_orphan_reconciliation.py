"""
F3 completion — erasure failures must be observable, and orphans findable.

Two gaps remained after the post_delete erasure landed:

  * the signal path discarded the result of `erase_uploaded_file`, so a failure
    after commit produced only a prose log line, while `purge_user_data` emitted
    a structured ERASURE_INCOMPLETE for the same condition. The row naming the
    file is already gone by then, so without the event the orphan is unfindable.

  * `transaction.on_commit` runs in-process. If the container dies between the
    commit and the callback — a redeploy, an OOM kill — the callback never runs
    and there is not even a log line. The row is gone and the bytes remain, with
    no trace at all.

The first is fixed by emitting the event. The second cannot be fixed by
on_commit and is not pretended away: `reconcile_orphaned_files` is the sweep
that detects the result afterwards.
"""
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.test import TestCase
from unittest.mock import patch

from apps.ai_insights.models import AIModel, ModelPrediction
from apps.medical_records.models import MedicalRecord

User = get_user_model()


class _Files(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'orph_patient', email='orph_patient@test.invalid', password='pw', role='patient')

    def _record(self, name='panel.pdf'):
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')
        record.file.save(name, ContentFile(b'%PDF-1.4 x'), save=True)
        return record


class FailureIsObservableTests(_Files):
    """A failure after commit must be machine-readable, not just prose."""

    def test_a_storage_failure_emits_erasure_incomplete(self):
        """ACCEPTANCE — the signal path used to discard the result."""
        record = self._record()

        with patch('django.core.files.storage.FileSystemStorage.delete',
                   side_effect=OSError('storage unavailable')):
            with self.assertLogs('healthcompass.ops', level='ERROR') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    record.delete()

        self.assertTrue(any('ERASURE_INCOMPLETE' in line for line in logs.output))

    def test_the_event_names_the_record_so_the_orphan_is_traceable(self):
        record = self._record()
        pk = str(record.pk)

        with patch('django.core.files.storage.FileSystemStorage.delete',
                   side_effect=OSError('boom')):
            with self.assertLogs('healthcompass.ops', level='ERROR') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    record.delete()

        self.assertTrue(any(pk in line for line in logs.output))

    def test_the_event_carries_no_filename_or_clinical_text(self):
        """
        Filenames are user-supplied and can be disclosive on their own
        ("HIV-screening.pdf"). The event carries identifiers and counts.
        """
        record = self._record(name='HIV-screening-result.pdf')

        with patch('django.core.files.storage.FileSystemStorage.delete',
                   side_effect=OSError('boom')):
            with self.assertLogs('healthcompass.ops', level='ERROR') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    record.delete()

        ops_lines = [line for line in logs.output if 'ERASURE_INCOMPLETE' in line]
        joined = '\n'.join(ops_lines)
        self.assertNotIn('HIV', joined)
        self.assertNotIn('.pdf', joined)

    def test_a_successful_deletion_emits_nothing(self):
        record = self._record()
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertNoLogs('healthcompass.ops', level='ERROR'):
                record.delete()


class ReconciliationTests(_Files):
    """
    The sweep that covers the crash window on_commit cannot.

    Runs against an isolated MEDIA_ROOT. The suite otherwise writes into the
    real media directory — a full run of this repository leaves well over a
    thousand files there — so without isolation these tests would be judging
    accumulated test debris and could not tell an orphan from a leftover.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile

        from django.test import override_settings

        cls._media = tempfile.TemporaryDirectory()
        cls._override = override_settings(MEDIA_ROOT=cls._media.name)
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._override.disable()
        cls._media.cleanup()

    def _orphan(self, prefix='medical_records/2020/01', name='ghost.pdf'):
        """A file on storage that no row references — the crash-window result."""
        path = default_storage.save(f'{prefix}/{name}', ContentFile(b'%PDF-1.4 orphan'))
        self._age(path)
        return path

    @staticmethod
    def _age(path):
        """Backdate the file past the command's safety window."""
        import os
        import time

        full = default_storage.path(path)
        old = time.time() - 60 * 60 * 24 * 7
        os.utime(full, (old, old))

    def test_an_orphan_is_reported(self):
        """ACCEPTANCE — previously nothing could find these."""
        from io import StringIO

        path = self._orphan()
        out = StringIO()
        call_command('reconcile_orphaned_files', stdout=out)

        self.assertIn('orphan', out.getvalue().lower())
        self.assertTrue(default_storage.exists(path), 'reporting must not delete')

    def test_a_referenced_file_is_never_reported(self):
        from io import StringIO

        record = self._record()
        self._age(record.file.name)

        out = StringIO()
        call_command('reconcile_orphaned_files', stdout=out)

        self.assertNotIn(record.file.name, out.getvalue())
        self.assertTrue(default_storage.exists(record.file.name))

    def test_delete_removes_only_the_orphan(self):
        record = self._record()
        self._age(record.file.name)
        orphan = self._orphan(name='ghost2.pdf')

        call_command('reconcile_orphaned_files', delete=True, verbosity=0)

        self.assertFalse(default_storage.exists(orphan))
        self.assertTrue(default_storage.exists(record.file.name),
                        'a referenced file was deleted')

    def test_a_recent_file_is_never_treated_as_an_orphan(self):
        """An upload in flight has bytes before its row is committed."""
        path = default_storage.save('medical_records/2020/01/fresh.pdf',
                                    ContentFile(b'%PDF-1.4 new'))

        call_command('reconcile_orphaned_files', delete=True, verbosity=0)

        self.assertTrue(default_storage.exists(path))

    def test_it_covers_every_file_bearing_model(self):
        """
        Not just MedicalRecord: prediction inputs, model artifacts and profile
        pictures orphan the same way.
        """
        from apps.medical_records.management.commands.reconcile_orphaned_files import (
            FILE_FIELDS,
        )

        covered = {dotted for _, dotted, _, _ in FILE_FIELDS}
        self.assertEqual(covered, {
            'medical_records.MedicalRecord',
            'ai_insights.ModelPrediction',
            'ai_insights.AIModel',
            'accounts.CustomUser',
        })

    def test_a_prediction_input_orphan_is_found(self):
        model = AIModel.objects.create(
            data_scientist=None, is_system=True, name='Sys', description='d')
        prediction = ModelPrediction.objects.create(model=model, patient=self.patient)
        prediction.input_file.save('eeg.parquet', ContentFile(b'PAR1'), save=True)
        name = prediction.input_file.name

        ModelPrediction.objects.filter(pk=prediction.pk).update(input_file='')
        self._age(name)

        call_command('reconcile_orphaned_files', delete=True, verbosity=0)
        self.assertFalse(default_storage.exists(name))

    def test_running_it_twice_is_harmless(self):
        self._orphan(name='ghost3.pdf')
        call_command('reconcile_orphaned_files', delete=True, verbosity=0)
        call_command('reconcile_orphaned_files', delete=True, verbosity=0)

    def test_a_clean_installation_reports_agreement(self):
        from io import StringIO

        out = StringIO()
        call_command('reconcile_orphaned_files', stdout=out)
        self.assertIn('agree', out.getvalue())

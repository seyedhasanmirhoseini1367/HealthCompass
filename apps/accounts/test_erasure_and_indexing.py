"""
REGRESSION — NEW-19 (incomplete erasure) and NEW-20 (indexing lost on deploy).

NEW-19 · Right-to-erasure left files behind
--------------------------------------------
`purge_user_data` deleted the profile picture and MedicalRecord.file, but not
ModelPrediction.input_file (uploaded EEG/images submitted for inference) or
AIModel.model_file for data-scientist accounts. The DB rows cascaded away, so
those files became unreachable through `_user_can_access_media` — which checks a
row that no longer exists — and could not even be found and removed through the
application afterwards. An erasure request that leaves the data present is not
fulfilled.

Deletion also happened INSIDE `transaction.atomic()`. If the transaction rolled
back the rows returned while the bytes were already gone irreversibly. Side
effects with no compensating action do not belong in a transaction.

NEW-20 · Indexing work evaporates on redeploy
----------------------------------------------
Indexing is dispatched to an in-process ThreadPoolExecutor with an unbounded
in-memory queue. A large import queues hundreds of jobs; a redeploy discards
them and those records are never chunked. `retry_failed_embeddings` cannot help
— it finds chunks with a NULL embedding, and a record that never reached
DocumentProcessor has no chunk row at all. The patient sees the record; the
assistant denies it exists.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.ai_insights.models import AIModel, ModelPrediction
from apps.medical_records.models import MedicalRecord

User = get_user_model()


class ErasureCompletenessTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='erase', password='pw-test-only', email='e@example.com')

    def _record_with_file(self):
        record = MedicalRecord.objects.create(
            patient=self.user, title='Doc', record_type='lab_result')
        record.file.save('doc.pdf', ContentFile(b'%PDF-1.4 fake'), save=True)
        return record

    def _prediction_with_file(self):
        model = AIModel.objects.create(
            data_scientist=self.user, name='M', description='d')
        pred = ModelPrediction.objects.create(model=model, patient=self.user)
        pred.input_file.save('signal.csv', ContentFile(b'a,b\n1,2\n'), save=True)
        return pred

    def test_medical_record_files_are_deleted(self):
        record = self._record_with_file()
        storage, name = record.file.storage, record.file.name
        from apps.accounts.services import purge_user_data
        purge_user_data(self.user)
        self.assertFalse(storage.exists(name))

    def test_prediction_input_files_are_deleted(self):
        """ACCEPTANCE — NEW-19. These were left on disk, orphaned and unreachable."""
        pred = self._prediction_with_file()
        storage, name = pred.input_file.storage, pred.input_file.name
        from apps.accounts.services import purge_user_data
        purge_user_data(self.user)
        self.assertFalse(storage.exists(name))

    def test_data_scientist_model_files_are_deleted(self):
        model = AIModel.objects.create(
            data_scientist=self.user, name='M2', description='d')
        model.model_file.save('m.onnx', ContentFile(b'fake-onnx'), save=True)
        storage, name = model.model_file.storage, model.model_file.name
        from apps.accounts.services import purge_user_data
        purge_user_data(self.user)
        self.assertFalse(storage.exists(name))

    def test_every_owned_file_goes_in_one_pass(self):
        record = self._record_with_file()
        pred = self._prediction_with_file()
        paths = [(record.file.storage, record.file.name),
                 (pred.input_file.storage, pred.input_file.name)]

        from apps.accounts.services import purge_user_data
        purge_user_data(self.user)

        remaining = [name for storage, name in paths if storage.exists(name)]
        self.assertEqual(remaining, [], f'files survived erasure: {remaining}')

    def test_the_user_row_is_gone(self):
        from apps.accounts.services import purge_user_data
        pk = self.user.pk
        purge_user_data(self.user)
        self.assertFalse(User.objects.filter(pk=pk).exists())

    def test_a_failed_file_delete_is_reported_not_swallowed(self):
        """A silently skipped file is an unfulfilled erasure request."""
        from unittest.mock import patch

        self._record_with_file()
        from apps.accounts.services import purge_user_data
        with patch('django.db.models.fields.files.FieldFile.delete',
                   side_effect=OSError('storage unavailable')):
            with self.assertLogs('healthcompass.ops', level='ERROR') as logs:
                purge_user_data(self.user)
        self.assertTrue(any('ERASURE_INCOMPLETE' in line for line in logs.output))

    def test_file_deletion_happens_outside_the_transaction(self):
        """
        Structural: files are removed after the commit. Inside the atomic block,
        a rollback would restore the rows while the bytes were already gone.
        """
        import inspect
        from apps.accounts import services
        source = inspect.getsource(services.purge_user_data)
        atomic_at = source.index('transaction.atomic')
        delete_at = source.index('field_file.delete')
        self.assertGreater(delete_at, atomic_at,
                           'file deletion must follow the transaction, not sit inside it')


class UnindexedRecordSweepTests(TestCase):
    """NEW-20 — records whose indexing never ran must be findable and fixable."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='idx', password='pw-test-only', email='i@example.com')

    def _stale_record(self, indexed=False):
        """
        A record uploaded two hours ago, past the sweep's safety window.

        The post_save signal indexes synchronously under test settings, so the
        row comes back already stamped. `indexed=False` clears the stamp with a
        queryset .update() — no signal, no re-index — which is exactly the state
        a redeploy leaves behind: the row exists, the indexing never ran.
        """
        record = MedicalRecord.objects.create(
            patient=self.user, title='Panel', record_type='lab_result',
            raw_text='Glucose: 5.2 mmol/L')
        MedicalRecord.objects.filter(pk=record.pk).update(
            uploaded_at=timezone.now() - timedelta(hours=2),
            indexed_at=timezone.now() if indexed else None)
        record.refresh_from_db()
        return record

    def test_unindexed_records_are_identifiable(self):
        """ACCEPTANCE — NEW-20. Previously nothing distinguished them."""
        self._stale_record(indexed=False)
        self.assertEqual(
            MedicalRecord.objects.filter(indexed_at__isnull=True).count(), 1)

    def test_dry_run_changes_nothing(self):
        record = self._stale_record(indexed=False)
        call_command('reindex_unindexed_records', dry_run=True, verbosity=0)
        record.refresh_from_db()
        self.assertIsNone(record.indexed_at)

    def test_sweep_indexes_and_stamps_the_record(self):
        record = self._stale_record(indexed=False)
        call_command('reindex_unindexed_records', verbosity=0)
        record.refresh_from_db()
        self.assertIsNotNone(record.indexed_at)

    def test_already_indexed_records_are_left_alone(self):
        record = self._stale_record(indexed=True)
        stamp = record.indexed_at
        call_command('reindex_unindexed_records', verbosity=0)
        record.refresh_from_db()
        self.assertEqual(record.indexed_at, stamp)

    def test_recent_uploads_are_not_raced(self):
        """
        A record uploaded seconds ago may still be in the worker's queue.
        Reindexing it now would duplicate work the worker is already doing.
        """
        record = MedicalRecord.objects.create(
            patient=self.user, title='Fresh', record_type='lab_result',
            raw_text='Glucose: 5.2 mmol/L')
        MedicalRecord.objects.filter(pk=record.pk).update(indexed_at=None)
        call_command('reindex_unindexed_records', verbosity=0)
        record.refresh_from_db()
        self.assertIsNone(record.indexed_at)

    def test_sweep_is_idempotent(self):
        self._stale_record(indexed=False)
        call_command('reindex_unindexed_records', verbosity=0)
        first = MedicalRecord.objects.filter(indexed_at__isnull=True).count()
        call_command('reindex_unindexed_records', verbosity=0)
        self.assertEqual(
            MedicalRecord.objects.filter(indexed_at__isnull=True).count(), first)

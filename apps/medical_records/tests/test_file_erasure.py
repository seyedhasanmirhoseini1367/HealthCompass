"""
REGRESSION — F3: deleting a record left its file on storage.

`purge_user_data` was the only code in the application that deleted a file, and
it is reached by exactly one view. Every other path deleted the row and left the
bytes:

    web  record_delete   medical_records/views.py   record.delete()
    API  record_delete   api/views/records.py       record.delete()
    admin single delete                             obj.delete()
    admin bulk delete                               queryset.delete()
    user deletion                                   cascade

The last two never call `Model.delete()`, so no override and no service call
could have covered them — which is why the fix is a `post_delete` receiver, and
why these tests exercise each path separately rather than trusting one of them
to stand for the rest.

The orphaned file was unreachable rather than exposed: `resolve_media_owner`
attributes a path through the MedicalRecord row, so once the row is gone
`can_access_media` refuses it. This was a retention and erasure defect, not an
access-control one — the bytes persisted after a patient was told they were
deleted.

on_commit note: file removal is deferred until the deletion commits, and a
TestCase never commits, so every test that asserts on storage wraps the delete
in `captureOnCommitCallbacks`. Without that these tests would pass vacuously
against an implementation that deleted nothing.
"""
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from apps.medical_records.models import MedicalRecord

User = get_user_model()


class _Records(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'fe_patient', email='fe_patient@test.invalid', password='pw', role='patient')

    def _record(self, title='Panel', payload=b'%PDF-1.4 x'):
        record = MedicalRecord.objects.create(
            patient=self.patient, title=title, record_type='lab_result')
        record.file.save('panel.pdf', ContentFile(payload), save=True)
        return record

    def _where(self, record):
        return record.file.storage, record.file.name

    def assertGone(self, storage, name):
        self.assertFalse(storage.exists(name), f'file survived deletion: {name}')

    def assertPresent(self, storage, name):
        self.assertTrue(storage.exists(name), f'file missing before deletion: {name}')


class ServiceLayerTests(_Records):
    """The primitive every path goes through."""

    def test_the_file_is_removed(self):
        record = self._record()
        storage, name = self._where(record)
        self.assertPresent(storage, name)

        with self.captureOnCommitCallbacks(execute=True):
            record.delete()

        self.assertGone(storage, name)

    def test_deleting_a_record_with_no_file_is_harmless(self):
        record = MedicalRecord.objects.create(
            patient=self.patient, title='No file', record_type='lab_result')
        with self.captureOnCommitCallbacks(execute=True):
            record.delete()          # must not raise
        self.assertEqual(MedicalRecord.objects.count(), 0)

    def test_an_already_missing_file_is_not_an_error(self):
        record = self._record()
        storage, name = self._where(record)
        storage.delete(name)         # vanished behind our back

        with self.captureOnCommitCallbacks(execute=True):
            record.delete()          # must not raise

    def test_erasure_is_idempotent(self):
        from apps.accounts.services import erase_uploaded_file

        record = self._record()
        storage, name = self._where(record)

        self.assertTrue(erase_uploaded_file(storage, name, label='t'))
        self.assertTrue(erase_uploaded_file(storage, name, label='t'))
        self.assertGone(storage, name)

    def test_only_the_deleted_records_file_is_touched(self):
        keep = self._record(title='Keep', payload=b'%PDF-1.4 keep')
        drop = self._record(title='Drop', payload=b'%PDF-1.4 drop')
        keep_where, drop_where = self._where(keep), self._where(drop)

        with self.captureOnCommitCallbacks(execute=True):
            drop.delete()

        self.assertGone(*drop_where)
        self.assertPresent(*keep_where)

    def test_a_rolled_back_deletion_keeps_the_file(self):
        """
        The reason removal is deferred to on_commit. Deleting inside the
        transaction would destroy the bytes for a row that came back.
        """
        from django.db import transaction

        record = self._record()
        storage, name = self._where(record)
        # Captured before deleting: Model.delete() sets instance.pk to None, so
        # reading it afterwards would query for pk=None and always find nothing.
        pk = record.pk

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    record.delete()
                    raise RuntimeError('rollback')
            except RuntimeError:
                pass

        self.assertEqual(callbacks, [],
                         'the erasure was scheduled despite the rollback')
        self.assertPresent(storage, name)
        self.assertTrue(MedicalRecord.objects.filter(pk=pk).exists())


class WebPathTests(_Records):

    def test_the_web_delete_view_removes_the_file(self):
        record = self._record()
        storage, name = self._where(record)
        self.client.force_login(self.patient)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('medical_records:delete', args=[record.pk]))

        self.assertIn(response.status_code, (302, 200))
        self.assertGone(storage, name)


class ApiPathTests(_Records):

    def test_the_api_delete_endpoint_removes_the_file(self):
        from rest_framework.test import APIClient

        record = self._record()
        storage, name = self._where(record)
        client = APIClient()
        client.force_authenticate(user=self.patient)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.delete(
                reverse('api:record_delete', args=[str(record.pk)]))

        self.assertEqual(response.status_code, 204)
        self.assertGone(storage, name)


class AdminPathTests(_Records):
    """Bulk delete is the path no service call could have covered."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(
            'fe_admin', email='fe_admin@test.invalid', password='pw-admin-1')
        self.client.force_login(self.admin)

    def test_admin_single_delete_removes_the_file(self):
        record = self._record()
        storage, name = self._where(record)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse('admin:medical_records_medicalrecord_delete', args=[record.pk]),
                {'post': 'yes'})

        self.assertGone(storage, name)

    def test_admin_bulk_delete_removes_every_file(self):
        """ACCEPTANCE — F3. queryset.delete() never calls Model.delete()."""
        records = [self._record(title=f'Bulk {i}', payload=f'%PDF-{i}'.encode())
                   for i in range(3)]
        wheres = [self._where(r) for r in records]

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('admin:medical_records_medicalrecord_changelist'), {
                'action': 'delete_selected',
                'post': 'yes',
                '_selected_action': [str(r.pk) for r in records],
            })

        self.assertEqual(MedicalRecord.objects.count(), 0)
        for storage, name in wheres:
            self.assertGone(storage, name)

    def test_a_direct_queryset_delete_removes_the_file(self):
        record = self._record()
        storage, name = self._where(record)

        with self.captureOnCommitCallbacks(execute=True):
            MedicalRecord.objects.filter(pk=record.pk).delete()

        self.assertGone(storage, name)


class CascadeTests(_Records):
    """Deleting the owner must not leave their files behind either."""

    def test_user_deletion_cascade_removes_record_files(self):
        """ACCEPTANCE — F3. The cascade never calls Model.delete()."""
        record = self._record()
        storage, name = self._where(record)

        with self.captureOnCommitCallbacks(execute=True):
            self.patient.delete()

        self.assertGone(storage, name)

    def test_the_erasure_service_still_removes_everything(self):
        """purge_user_data's own guarantee is unchanged."""
        from apps.accounts.services import purge_user_data

        record = self._record()
        storage, name = self._where(record)

        with self.captureOnCommitCallbacks(execute=True):
            purge_user_data(self.patient)

        self.assertGone(storage, name)
        self.assertFalse(User.objects.filter(pk=self.patient.pk).exists())

    def test_the_overlap_reports_no_false_failure(self):
        """
        A record file is reached by both the cascade and purge's own loop.
        The second attempt must count as success, not as an incomplete erasure.
        """
        from apps.accounts.services import purge_user_data

        self._record()
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertNoLogs('healthcompass.ops', level='ERROR'):
                purge_user_data(self.patient)


class OrphanReachabilityTests(_Records):
    """
    Documents why this was a retention defect rather than an exposure one: an
    orphaned file cannot be attributed to an owner, so media access refuses it.
    """

    def test_an_orphaned_file_is_not_downloadable(self):
        from apps.accounts.authz import can_access_media

        record = self._record()
        _, name = self._where(record)

        MedicalRecord.objects.filter(pk=record.pk).delete()   # no on_commit here

        self.assertFalse(can_access_media(self.patient, name))

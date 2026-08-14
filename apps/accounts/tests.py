from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.contrib.auth import get_user_model

User = get_user_model()


# ── purge_user_data ───────────────────────────────────────────────────────────

class PurgeUserDataTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='purgetest', email='purge@example.com', password='pw'
        )

    def test_user_removed_from_db(self):
        from apps.accounts.services import purge_user_data
        pk = self.user.pk
        purge_user_data(self.user)
        self.assertFalse(User.objects.filter(pk=pk).exists())

    def test_medical_records_removed_by_cascade(self):
        from apps.accounts.services import purge_user_data
        from apps.medical_records.models import MedicalRecord
        MedicalRecord.objects.create(
            patient=self.user, title='PHI Record', record_type='lab_result'
        )
        pk = self.user.pk
        purge_user_data(self.user)
        self.assertFalse(MedicalRecord.objects.filter(patient_id=pk).exists())

    def test_purge_with_no_files_is_clean(self):
        """User with no profile picture and no record files purges without error."""
        from apps.accounts.services import purge_user_data
        pk = self.user.pk
        purge_user_data(self.user)
        self.assertFalse(User.objects.filter(pk=pk).exists())

    def test_profile_picture_file_is_erased(self):
        """
        Asserts the outcome, not the mechanism.

        This used to assert `.delete(save=False)` was called on the FieldFile.
        Erasure now goes through the shared `erase_uploaded_file`, so pinning
        the old call made the test fail while the file was still being removed —
        it was measuring how, not whether.
        """
        from django.core.files.base import ContentFile

        from apps.accounts.services import purge_user_data

        self.user.profile_picture.save(
            'pic.png', ContentFile(b'\x89PNG\r\n\x1a\n'), save=True)
        storage, name = self.user.profile_picture.storage, self.user.profile_picture.name
        self.assertTrue(storage.exists(name))

        with self.captureOnCommitCallbacks(execute=True):
            purge_user_data(self.user)

        self.assertFalse(storage.exists(name))

    def test_record_file_is_erased(self):
        """Same: the file is gone afterwards, however that is achieved."""
        from django.core.files.base import ContentFile

        from apps.accounts.services import purge_user_data
        from apps.medical_records.models import MedicalRecord

        record = MedicalRecord.objects.create(
            patient=self.user, title='Scan', record_type='imaging')
        record.file.save('scan.pdf', ContentFile(b'%PDF-1.4 x'), save=True)
        storage, name = record.file.storage, record.file.name
        self.assertTrue(storage.exists(name))

        with self.captureOnCommitCallbacks(execute=True):
            purge_user_data(self.user)

        self.assertFalse(storage.exists(name))

    def test_chat_sessions_removed_by_cascade(self):
        from apps.accounts.services import purge_user_data
        from apps.rag_assistant.models import ChatSession
        ChatSession.objects.create(patient=self.user, title='Session 1')
        pk = self.user.pk
        purge_user_data(self.user)
        self.assertFalse(ChatSession.objects.filter(patient_id=pk).exists())


# ── EmailOrUsernameBackend ────────────────────────────────────────────────────

class EmailOrUsernameBackendTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='backendtest',
            email='backend@example.com',
            password='pw123',
        )

    def _auth(self, identifier, password):
        from apps.accounts.backends import EmailOrUsernameBackend
        return EmailOrUsernameBackend().authenticate(None, username=identifier, password=password)

    def test_login_by_email(self):
        self.assertEqual(self._auth('backend@example.com', 'pw123'), self.user)

    def test_login_by_email_case_insensitive(self):
        self.assertEqual(self._auth('BACKEND@EXAMPLE.COM', 'pw123'), self.user)

    def test_login_by_username(self):
        self.assertEqual(self._auth('backendtest', 'pw123'), self.user)

    def test_login_by_username_case_insensitive(self):
        self.assertEqual(self._auth('BACKENDTEST', 'pw123'), self.user)

    def test_wrong_password_returns_none(self):
        self.assertIsNone(self._auth('backend@example.com', 'wrong'))

    def test_nonexistent_user_returns_none(self):
        self.assertIsNone(self._auth('nobody@example.com', 'pw'))

    def test_inactive_user_returns_none(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertIsNone(self._auth('backend@example.com', 'pw123'))

    def test_timing_defence_runs_hasher_on_miss(self):
        """Calling authenticate for an unknown user must invoke set_password
        so timing is indistinguishable from a wrong-password attempt."""
        with patch.object(User, 'set_password') as mock_set:
            self._auth('ghost@example.com', 'irrelevant')
        mock_set.assert_called_once_with('irrelevant')

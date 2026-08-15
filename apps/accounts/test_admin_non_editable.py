"""
Clinical data in the admin: deletable, never rewritten.

Why both halves need pinning
----------------------------
The first version of `NonEditablePhiAdmin` blocked deletion as well, and the
file-erasure suite caught it: `test_admin_bulk_delete_removes_every_file` began
failing because there was no longer an admin delete to test. That was the right
failure. Blocking deletion removed the only path by which a controller can erase
one record belonging to the wrong account, and it did so while the F3 guarantee
— post_delete erases the underlying file, including on bulk delete — was built
precisely for that path.

The distinction the class draws is between two different acts:

  * modification makes a record assert something its source never said, with no
    provenance and no way for anyone downstream to notice;
  * deletion removes the assertion and claims nothing.

Only the first is falsification. These tests exist so that "read-only" is not
reintroduced as a blanket, and so the no-edit half is not quietly relaxed either.
"""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.medical_records.models import MedicalRecord
from apps.rag_assistant.models import (ChatSession, MedicalChunk,
                                       MedicalDocument, QueryLog)

User = get_user_model()

#: Every model whose admin carries the rule.
GOVERNED = [MedicalRecord, QueryLog, ChatSession, MedicalDocument, MedicalChunk]


class RuleTests(TestCase):

    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            'ne_admin', email='ne_admin@test.invalid', password='pw-admin-1')

    def _admin(self, model):
        return admin.site._registry[model]

    def test_nothing_clinical_can_be_edited_in_the_admin(self):
        for model in GOVERNED:
            with self.subTest(model=model.__name__):
                self.assertFalse(
                    self._admin(model).has_change_permission(None),
                    f'{model.__name__} became editable — a hand-edit changes what a '
                    f'document asserts with no provenance and no correction trail')

    def test_nothing_clinical_can_be_created_by_hand(self):
        for model in GOVERNED:
            with self.subTest(model=model.__name__):
                self.assertFalse(
                    self._admin(model).has_add_permission(None),
                    f'{model.__name__} became creatable — that fabricates a document '
                    f'no source produced')

    def test_deletion_stays_available(self):
        """
        ACCEPTANCE — this is the half that was wrong the first time.

        Erasure is a right the controller must be able to exercise, and it is
        safe here because post_delete takes the file with the row.
        """
        request = _RequestFor(self.admin_user)
        for model in GOVERNED:
            with self.subTest(model=model.__name__):
                self.assertTrue(
                    self._admin(model).has_delete_permission(request),
                    f'{model.__name__} can no longer be deleted — that removes the '
                    f'only path for erasing one record, and the file-erasure '
                    f'guarantee exists for exactly this path')


class ErasureStillWorksTests(TestCase):
    """The rule is only safe because deletion still erases the bytes."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'ne_patient', email='ne_patient@test.invalid', password='pw', role='patient')
        self.admin_user = User.objects.create_superuser(
            'ne_admin2', email='ne_admin2@test.invalid', password='pw-admin-1')
        self.client.force_login(self.admin_user)

    def test_an_admin_can_still_delete_a_record_through_the_admin(self):
        from django.core.files.base import ContentFile
        from django.urls import reverse

        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')
        record.file.save('panel.pdf', ContentFile(b'%PDF-1.4 test'), save=True)
        storage, name = record.file.storage, record.file.name

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse('admin:medical_records_medicalrecord_delete', args=[record.pk]),
                {'post': 'yes'})

        self.assertFalse(MedicalRecord.objects.filter(pk=record.pk).exists())
        self.assertFalse(storage.exists(name), 'the file outlived the row')

    def test_the_admin_change_form_does_not_offer_a_save(self):
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result')

        from django.urls import reverse
        response = self.client.get(
            reverse('admin:medical_records_medicalrecord_change', args=[record.pk]))

        # Django renders the change view read-only rather than 403-ing, so the
        # assertion is on the absence of the submit row, not on a status code.
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')


class _RequestFor:
    """Minimal stand-in — these permission hooks only ever read `user`."""

    def __init__(self, user):
        self.user = user

"""
REGRESSION — DM-1: ingesting the same artifact twice must not duplicate it.

Re-uploading a document created a second MedicalRecord and a second full set of
ParsedLabValue rows. Three consequences, in increasing order of seriousness:

  1. The patient saw every value twice.
  2. conflict_service classified the result as a `duplicate` clinical finding —
     the system reporting its own ingestion defect to the patient as a data
     conflict.
  3. Every trajectory calculation counted the reading twice.

What identity means here, and what it deliberately does not
-----------------------------------------------------------
The scope is the **artifact**, not the facts. Two blood draws that happen to
carry identical values on different dates are two real medical events and must
both survive; only the same bytes arriving twice is a duplicate. So the
fingerprint covers the ingested content, and nothing de-duplicates
ParsedLabValue rows.

For Kanta the fingerprint is per DOCUMENT, not per upload: a bundle produces one
record per document, so hashing the XML would make every document collide with
its siblings. Re-importing an export that overlaps a previous one is the normal
case there.

Records created before this field existed carry content_hash='' and are exempt,
which is why the uniqueness constraint is partial. They are not retroactively
de-duplicated and not invalidated.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.medical_records.models import MedicalRecord, ParsedLabValue
from apps.medical_records.services import (
    MedicalRecordService, content_fingerprint, find_duplicate,
)

TEXT = 'Glucose: 5.2 mmol/L\nCreatinine: 78 umol/L'


class FingerprintTests(TestCase):
    """The identity function itself."""

    def test_same_bytes_produce_the_same_digest(self):
        self.assertEqual(content_fingerprint(b'abc'), content_fingerprint(b'abc'))

    def test_different_bytes_produce_different_digests(self):
        self.assertNotEqual(content_fingerprint(b'abc'), content_fingerprint(b'abd'))

    def test_text_and_bytes_agree(self):
        self.assertEqual(content_fingerprint('abc'), content_fingerprint(b'abc'))

    def test_structures_are_order_independent(self):
        """A Kanta document must fingerprint the same however its keys are ordered."""
        self.assertEqual(content_fingerprint({'a': 1, 'b': 2}),
                         content_fingerprint({'b': 2, 'a': 1}))

    def test_structural_difference_changes_the_digest(self):
        self.assertNotEqual(content_fingerprint({'a': 1}), content_fingerprint({'a': 2}))

    def test_empty_content_has_no_fingerprint(self):
        """
        Otherwise every empty upload would collide into a single row, which is
        worse than not de-duplicating them.
        """
        for empty in (None, '', b'', '   '):
            with self.subTest(value=empty):
                self.assertEqual(content_fingerprint(empty), '')


class TextIngestionTests(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='idem-text', password='pw-test-only', email='t@example.com')

    def test_reuploading_identical_text_creates_one_record(self):
        """ACCEPTANCE — DM-1."""
        first  = MedicalRecordService.create_from_text(self.user, TEXT, title_override='Panel')
        second = MedicalRecordService.create_from_text(self.user, TEXT, title_override='Panel')

        self.assertEqual(MedicalRecord.objects.filter(patient=self.user).count(), 1)
        self.assertTrue(second.get('duplicate'))
        self.assertEqual(first['record'].pk, second['record'].pk)

    def test_reupload_does_not_duplicate_lab_values(self):
        """The damage was never the record row — it was the values under it."""
        MedicalRecordService.create_from_text(self.user, TEXT, title_override='Panel')
        after_first = ParsedLabValue.objects.filter(record__patient=self.user).count()
        MedicalRecordService.create_from_text(self.user, TEXT, title_override='Panel')
        self.assertEqual(
            ParsedLabValue.objects.filter(record__patient=self.user).count(), after_first)

    def test_different_content_still_creates_a_second_record(self):
        MedicalRecordService.create_from_text(self.user, TEXT, title_override='A')
        MedicalRecordService.create_from_text(
            self.user, 'Glucose: 7.8 mmol/L', title_override='B')
        self.assertEqual(MedicalRecord.objects.filter(patient=self.user).count(), 2)

    def test_a_repeated_measurement_is_not_a_duplicate_upload(self):
        """
        The distinction the whole design rests on: identical VALUES reported in
        two genuinely different documents are two medical events.
        """
        MedicalRecordService.create_from_text(
            self.user, 'Panel A 2026-01-01\nGlucose: 5.2 mmol/L', title_override='Jan')
        MedicalRecordService.create_from_text(
            self.user, 'Panel B 2026-06-01\nGlucose: 5.2 mmol/L', title_override='Jun')
        self.assertEqual(MedicalRecord.objects.filter(patient=self.user).count(), 2)

    def test_the_stored_record_carries_its_fingerprint(self):
        result = MedicalRecordService.create_from_text(self.user, TEXT, title_override='P')
        self.assertEqual(result['record'].content_hash, content_fingerprint(TEXT))


class PatientIsolationTests(TestCase):
    """The same document belonging to two people is two records."""

    def test_identical_content_from_two_patients_is_not_deduplicated(self):
        a = get_user_model().objects.create_user(
            username='idem-a', password='pw-test-only', email='a@example.com')
        b = get_user_model().objects.create_user(
            username='idem-b', password='pw-test-only', email='b@example.com')

        MedicalRecordService.create_from_text(a, TEXT, title_override='P')
        MedicalRecordService.create_from_text(b, TEXT, title_override='P')

        self.assertEqual(MedicalRecord.objects.filter(patient=a).count(), 1)
        self.assertEqual(MedicalRecord.objects.filter(patient=b).count(), 1)

    def test_find_duplicate_never_crosses_patients(self):
        a = get_user_model().objects.create_user(
            username='idem-c', password='pw-test-only', email='c@example.com')
        b = get_user_model().objects.create_user(
            username='idem-d', password='pw-test-only', email='d@example.com')
        MedicalRecordService.create_from_text(a, TEXT, title_override='P')
        self.assertIsNone(find_duplicate(b, content_fingerprint(TEXT)))


class DatabaseGuaranteeTests(TestCase):
    """
    The constraint, not just the lookup.

    Two concurrent identical uploads would both pass the application-level check;
    this is what actually prevents the second row.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='idem-db', password='pw-test-only', email='db@example.com')

    def test_constraint_rejects_a_second_row_with_the_same_hash(self):
        digest = content_fingerprint('some content')
        MedicalRecord.objects.create(
            patient=self.user, title='first', record_type='lab_result',
            content_hash=digest)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MedicalRecord.objects.create(
                    patient=self.user, title='second', record_type='lab_result',
                    content_hash=digest)

    def test_blank_hashes_do_not_collide_with_each_other(self):
        """
        Historical rows all carry ''. A non-partial constraint would have made
        the migration impossible — every legacy row would collide.
        """
        for i in range(3):
            MedicalRecord.objects.create(
                patient=self.user, title=f'legacy {i}',
                record_type='lab_result', content_hash='')
        self.assertEqual(MedicalRecord.objects.filter(
            patient=self.user, content_hash='').count(), 3)


class KantaPerDocumentTests(TestCase):
    """A bundle produces one record per document; identity must be per document."""

    def test_documents_in_one_bundle_do_not_collide(self):
        """
        Hashing the upload rather than the document would have made every
        document in a bundle a duplicate of its siblings.
        """
        doc_a = {'title': 'Lab 2026-01', 'type': 'lab', 'sections': []}
        doc_b = {'title': 'Lab 2026-06', 'type': 'lab', 'sections': []}
        self.assertNotEqual(content_fingerprint(doc_a), content_fingerprint(doc_b))

    def test_the_same_document_fingerprints_identically_across_imports(self):
        doc = {'title': 'Lab 2026-01', 'type': 'lab',
               'sections': [{'entries': [{'kind': 'lab', 'name': 'Glucose'}]}]}
        self.assertEqual(content_fingerprint(doc), content_fingerprint(dict(doc)))

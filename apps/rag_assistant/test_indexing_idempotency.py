"""
REGRESSION — Phase 4: one indexing trigger, safe under repetition and concurrency.

`create_from_text` called `RAGService().index_record(record)` explicitly *in
addition to* the `post_save` signal that already indexes every saved record. The
PDF and Kanta paths did not — so the text/OCR path indexed twice and the others
once, an inconsistency nobody had noticed.

`DocumentProcessor.process_record` deletes every MedicalDocument for the record
and recreates it. Two concurrent runs therefore interleave as

    T1 delete → T2 delete → T1 create → T2 create

leaving **two** documents and two chunk sets for one record. Observed in the
development database: 10 records carry duplicate MedicalDocument rows.

Consequences: the same evidence occupies two slots of `top_k` at retrieval time,
and every chunk is embedded twice, doubling provider cost.

These tests pin the invariant directly — one record yields one document and one
chunk set, however many times and however concurrently indexing is invoked.
"""
import threading

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TransactionTestCase

from apps.medical_records.models import MedicalRecord
from apps.rag_assistant.models import MedicalChunk, MedicalDocument
from apps.rag_assistant.services.document_processor import DocumentProcessor


class SingleTriggerTests(TransactionTestCase):
    """Ingestion must index a record exactly once."""

    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='idem', password='pw-test-only', email='idem@example.com')

    def test_text_ingestion_produces_one_document(self):
        """
        ACCEPTANCE. The text path used to fire the signal AND call index_record
        directly, so this could produce two documents for one upload.
        """
        from apps.medical_records.services import MedicalRecordService

        result = MedicalRecordService.create_from_text(
            self.user, 'Glucose: 5.2 mmol/L\nCreatinine: 78 umol/L',
            title_override='Text upload')
        record = result['record']

        docs = MedicalDocument.objects.filter(record=record)
        self.assertEqual(docs.count(), 1,
                         f'expected exactly 1 document, found {docs.count()}')

    def test_no_ingestion_path_indexes_twice(self):
        """
        Structural: no service method may CALL index_record/process_record. The
        post_save signal is the single trigger, and a second explicit call is
        exactly what produced the duplicate documents.

        Parsed with ast rather than matched as text, so the prose explaining why
        the call was removed does not itself trip the check — and so a real
        reintroduction cannot hide inside a differently formatted expression.
        (utf-8-sig: a BOM has broken ast.parse in this repository before.)
        """
        import ast
        import pathlib

        import apps.medical_records.services as services_mod

        source = pathlib.Path(services_mod.__file__).read_text(encoding='utf-8-sig')
        tree = ast.parse(source)

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ('index_record', 'process_record'):
                    offenders.append(f'{node.func.attr} at line {node.lineno}')

        self.assertEqual(
            offenders, [],
            f'ingestion must not trigger indexing directly; the post_save signal '
            f'is the single trigger. Found: {offenders}')


class RepeatedIndexingTests(TransactionTestCase):
    """Re-indexing must converge, not accumulate."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='idem-rep', password='pw-test-only', email='r@example.com')
        self.record = MedicalRecord.objects.create(
            patient=self.user, title='Panel', record_type='lab_result',
            raw_text='Glucose: 5.2 mmol/L')

    def test_indexing_twice_leaves_one_document(self):
        processor = DocumentProcessor()
        processor.process_record(self.record)
        first = MedicalDocument.objects.filter(record=self.record).count()
        processor.process_record(self.record)
        second = MedicalDocument.objects.filter(record=self.record).count()

        self.assertEqual(first, 1)
        self.assertEqual(second, 1, 'a second index run must not add a document')

    def test_indexing_many_times_does_not_accumulate_chunks(self):
        processor = DocumentProcessor()
        for _ in range(4):
            processor.process_record(self.record)
        docs = MedicalDocument.objects.filter(record=self.record)
        self.assertEqual(docs.count(), 1)
        self.assertEqual(
            MedicalChunk.objects.filter(document__record=self.record).count(),
            MedicalChunk.objects.filter(document=docs.first()).count())


class ConcurrentIndexingTests(TransactionTestCase):
    """
    The race itself. TransactionTestCase (not TestCase) because the threads need
    real committed transactions rather than a shared wrapping one.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='idem-conc', password='pw-test-only', email='c@example.com')
        self.record = MedicalRecord.objects.create(
            patient=self.user, title='Concurrent panel', record_type='lab_result',
            raw_text='Glucose: 5.2 mmol/L\nCreatinine: 78 umol/L')
        MedicalDocument.objects.filter(record=self.record).delete()

    def test_concurrent_indexing_yields_at_most_one_document(self):
        """
        ACCEPTANCE. Delete-then-create interleaves without serialisation.

        Note on what this test can and cannot show: SQLite serialises writers, so
        under SQLite a losing thread raises OperationalError('database table is
        locked') rather than producing a duplicate. That is a *failed index*, not
        a duplicate, and it means this test cannot reproduce the PostgreSQL race
        on the development backend. The invariant asserted here — never more than
        one document per record — is the property that matters on either backend,
        and the lock in process_record() is what guarantees it on PostgreSQL.
        """
        errors = []
        barrier = threading.Barrier(4)

        def index():
            try:
                barrier.wait(timeout=10)
                DocumentProcessor().process_record(self.record)
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=index) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        docs = MedicalDocument.objects.filter(record=self.record)
        self.assertLessEqual(
            docs.count(), 1,
            f'concurrent indexing produced {docs.count()} documents for one record')
        # Any error must be a write-contention error, never data corruption.
        for exc in errors:
            self.assertIn('locked', str(exc).lower())

    def test_lock_prevents_a_second_concurrent_run(self):
        """
        Backend-independent proof of the serialisation itself: while the lock is
        held, a second invocation declines instead of entering the
        delete-then-create section.
        """
        from django.core.cache import cache

        lock_key = f'rag:index:{self.record.pk}'
        cache.add(lock_key, 1, timeout=60)
        try:
            created = DocumentProcessor().process_record(self.record)
            self.assertEqual(created, [],
                             'a second indexer must skip while the lock is held')
            self.assertEqual(
                MedicalDocument.objects.filter(record=self.record).count(), 0,
                'the skipped run must not have deleted or created anything')
        finally:
            cache.delete(lock_key)

    def test_lock_is_released_so_later_runs_succeed(self):
        """A held lock must never permanently block indexing."""
        DocumentProcessor().process_record(self.record)
        self.assertEqual(MedicalDocument.objects.filter(record=self.record).count(), 1)
        DocumentProcessor().process_record(self.record)
        self.assertEqual(MedicalDocument.objects.filter(record=self.record).count(), 1)

    def test_concurrent_indexing_does_not_orphan_chunks(self):
        """Chunks must belong to the surviving document, not a deleted one."""
        barrier = threading.Barrier(3)

        def index():
            try:
                barrier.wait(timeout=10)
                DocumentProcessor().process_record(self.record)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=index) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        doc_ids = set(MedicalDocument.objects.filter(record=self.record)
                      .values_list('id', flat=True))
        chunk_doc_ids = set(MedicalChunk.objects
                            .filter(patient=self.user)
                            .values_list('document_id', flat=True))
        self.assertTrue(chunk_doc_ids <= doc_ids,
                        'chunks reference a document that no longer exists')


class DocumentUniquenessTests(TransactionTestCase):
    """The invariant the whole phase protects."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='idem-uniq', password='pw-test-only', email='u@example.com')

    def test_one_record_yields_at_most_one_document(self):
        for rtype, text in (('lab_result', 'Glucose: 5.2 mmol/L'),
                            ('prescription', 'Metformin 1000 mg'),
                            ('diagnosis', 'Nephrology referral'),
                            ('other', 'Free text note')):
            with self.subTest(record_type=rtype):
                record = MedicalRecord.objects.create(
                    patient=self.user, title=f'{rtype} rec',
                    record_type=rtype, raw_text=text)
                DocumentProcessor().process_record(record)
                self.assertLessEqual(
                    MedicalDocument.objects.filter(record=record).count(), 1)

    def test_chunk_indexes_are_unique_within_a_document(self):
        record = MedicalRecord.objects.create(
            patient=self.user, title='Panel', record_type='lab_result',
            raw_text='Glucose: 5.2 mmol/L')
        DocumentProcessor().process_record(record)
        doc = MedicalDocument.objects.filter(record=record).first()
        if doc:
            indexes = list(MedicalChunk.objects.filter(document=doc)
                           .values_list('chunk_index', flat=True))
            self.assertEqual(len(indexes), len(set(indexes)))

"""
REGRESSION — trajectory answers must carry citations.

R1/R1b moved latest/previous/trend questions onto the trajectory path. That path
loads from ParsedLabValue -> MedicalRecord and never touches MedicalDocument, so
its source chunks had no `document_id`. `_build_sources()` keys every citation
off `document_id` and drops anything without one, so the whole recency family
answered correctly with an empty sources panel.

These tests assert the citation actually survives the trajectory path, and —
just as important — that a record with no indexed document does NOT get an
invented one.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.medical_records.models import MedicalRecord, ParsedLabValue
from apps.rag_assistant.services.document_processor import DocumentProcessor
from apps.rag_assistant.services.generation_service import _build_sources
from apps.rag_assistant.services.trajectory_service import TrajectoryService

User = get_user_model()
NO_AUTOINDEX = override_settings(RAG_AUTO_INDEX_SYNC=False)

TIMELINE = [
    ('Annual Check-Up 2024', '2024-03-11', '5.1', False),
    ('Follow-Up Panel 2025', '2025-04-02', '6.4', True),
    ('Metabolic Panel 2026', '2026-05-20', '7.8', True),
]


@NO_AUTOINDEX
class TrajectoryCitationTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='cite', email='cite@example.com', password='pw-cite-1',
        )
        self.records = []
        for title, day, value, abnormal in TIMELINE:
            self.records.append(self._add_lab(self.user, title, day, value, abnormal))

    # ── helpers ──────────────────────────────────────────────────────────────

    def _add_lab(self, user, title, day, value, abnormal=False,
                 parameter='Glucose', index=True):
        record = MedicalRecord.objects.create(
            patient=user, title=title, record_type='lab_result',
            record_date=date.fromisoformat(day),
        )
        ParsedLabValue.objects.create(
            record=record, parameter_name=parameter, value=str(value),
            unit='mmol/L', canonical_value=float(value), unit_known=True,
            is_abnormal=abnormal,
            measured_at=timezone.make_aware(
                timezone.datetime.fromisoformat(day + 'T09:00:00')),
        )
        if index:
            DocumentProcessor().process_record(record)
        return record

    def _sources(self, mode, user=None, query='my glucose'):
        _context, chunks = TrajectoryService().get_trajectory_context(
            user or self.user, query, temporal_mode=mode)
        return _build_sources(chunks), chunks

    # ── 1-3: citations exist for every temporal mode ─────────────────────────

    def test_latest_value_answers_carry_citations(self):
        sources, chunks = self._sources('latest')
        self.assertTrue(chunks, 'trajectory produced no source chunks at all')
        self.assertTrue(sources, 'latest-value answer produced zero citations')
        self.assertEqual(len(sources), len(TIMELINE))

    def test_previous_value_answers_carry_citations(self):
        sources, _ = self._sources('previous')
        self.assertTrue(sources, 'previous-value answer produced zero citations')

    def test_trend_answers_carry_citations(self):
        sources, _ = self._sources('trend')
        self.assertTrue(sources, 'trend answer produced zero citations')

    # ── citation content ─────────────────────────────────────────────────────

    def test_citations_name_the_records_the_values_came_from(self):
        sources, _ = self._sources('latest')
        titles = {s['title'] for s in sources}
        self.assertEqual(titles, {title for title, _d, _v, _a in TIMELINE})

    def test_citations_carry_document_and_record_identity_and_date(self):
        sources, _ = self._sources('latest')
        for source in sources:
            with self.subTest(title=source['title']):
                self.assertTrue(source['document_id'])
                self.assertTrue(source['record_id'])
                self.assertTrue(source['record_date'])
                self.assertEqual(source['document_type'], 'lab_result')

    def test_document_ids_are_real_documents_belonging_to_the_patient(self):
        """A citation must resolve to a row, not to a plausible-looking string."""
        from apps.rag_assistant.models import MedicalDocument

        sources, _ = self._sources('latest')
        for source in sources:
            with self.subTest(title=source['title']):
                self.assertTrue(
                    MedicalDocument.objects.filter(
                        id=source['document_id'], patient=self.user).exists())

    def test_metadata_preserves_the_analyte_and_original_unit(self):
        _sources, chunks = self._sources('latest')
        for chunk in chunks:
            meta = chunk['metadata']
            self.assertEqual(meta['parameter_name'], 'Glucose')
            self.assertEqual(meta['original_unit'], 'mmol/L')

    def test_offsets_are_attached_when_the_analyte_maps_to_one_chunk(self):
        _sources, chunks = self._sources('latest')
        located = [c for c in chunks if 'start_offset' in c['metadata']]
        self.assertTrue(located, 'no chunk offsets were resolved at all')
        for chunk in located:
            meta = chunk['metadata']
            self.assertIsInstance(meta['start_offset'], int)
            self.assertIsInstance(meta['end_offset'], int)
            self.assertLess(meta['start_offset'], meta['end_offset'])

    # ── 4: no fabrication ────────────────────────────────────────────────────

    def test_unindexed_record_gets_no_document_id(self):
        """A record with no MedicalDocument must not acquire an invented one."""
        lonely = User.objects.create_user(
            username='lonely', email='lonely@example.com', password='pw-lonely-1',
        )
        self._add_lab(lonely, 'Never Indexed', '2026-05-20', '7.8', index=False)

        _sources, chunks = self._sources('latest', user=lonely)
        self.assertTrue(chunks, 'the value itself should still be usable')
        for chunk in chunks:
            self.assertNotIn('document_id', chunk['metadata'])

    def test_unindexed_record_produces_no_citation_rather_than_a_broken_one(self):
        lonely = User.objects.create_user(
            username='lonely2', email='lonely2@example.com', password='pw-lonely-2',
        )
        self._add_lab(lonely, 'Never Indexed', '2026-05-20', '7.8', index=False)

        sources, _ = self._sources('latest', user=lonely)
        self.assertEqual(sources, [])

    def test_partially_indexed_history_cites_only_the_indexed_records(self):
        """Mixed data must degrade per-record, not all-or-nothing."""
        mixed = User.objects.create_user(
            username='mixed', email='mixed@example.com', password='pw-mixed-1',
        )
        self._add_lab(mixed, 'Indexed 2024', '2024-03-11', '5.1', index=True)
        self._add_lab(mixed, 'Unindexed 2026', '2026-05-20', '7.8', index=False)

        sources, chunks = self._sources('latest', user=mixed)
        self.assertEqual(len(chunks), 2)
        self.assertEqual([s['title'] for s in sources], ['Indexed 2024'])

    # ── 5: patient scoping ───────────────────────────────────────────────────

    def test_citations_never_reference_another_patients_document(self):
        from apps.rag_assistant.models import MedicalDocument

        other = User.objects.create_user(
            username='other', email='other@example.com', password='pw-other-1',
        )
        self._add_lab(other, 'Their Panel', '2026-05-20', '99.9')

        sources, _ = self._sources('latest')
        their_docs = set(
            MedicalDocument.objects.filter(patient=other).values_list('id', flat=True))
        for source in sources:
            self.assertNotIn(source['document_id'], {str(d) for d in their_docs})
            self.assertNotEqual(source['title'], 'Their Panel')

    def test_document_lookup_is_scoped_by_patient_not_only_by_record(self):
        """
        The enrichment query filters on patient as well as record id, so a
        mismatched record id cannot widen what a user is shown.
        """
        import inspect

        from apps.rag_assistant.services.trajectory_service import TrajectoryService as TS

        source = inspect.getsource(TS._attach_document_metadata)
        self.assertIn('patient=patient', source)
        self.assertEqual(source.count('patient=patient'), 2,
                         'both the document and chunk lookups must be patient-scoped')

    def test_another_patients_values_never_enter_the_trajectory(self):
        other = User.objects.create_user(
            username='other2', email='other2@example.com', password='pw-other-2',
        )
        self._add_lab(other, 'Their Panel', '2026-06-01', '99.9')

        _sources, chunks = self._sources('latest')
        joined = ' '.join(c['text'] for c in chunks)
        self.assertNotIn('99.9', joined)


@NO_AUTOINDEX
class NonTrajectoryCitationRegressionTests(TestCase):
    """6 — the paths that already worked must keep working."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='nontraj', email='nontraj@example.com', password='pw-nt-1',
        )

    def test_retrieval_path_citations_are_unchanged(self):
        """Shape of a citation built from ordinary retrieval metadata."""
        chunks = [{
            'text': 'Glucose: 7.8 mmol/L',
            'metadata': {
                'document_title': 'Metabolic Panel 2026',
                'document_type':  'lab_result',
                'document_id':    'doc-1',
                'record_id':      'rec-1',
                'record_date':    '2026-05-20',
                'start_offset':   10,
                'end_offset':     42,
            },
        }]
        sources = _build_sources(chunks)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['title'], 'Metabolic Panel 2026')
        self.assertEqual(sources[0]['document_id'], 'doc-1')
        self.assertEqual(sources[0]['start_offset'], 10)
        self.assertEqual(sources[0]['end_offset'], 42)

    def test_chunks_without_a_document_id_are_still_dropped(self):
        """The guarantee the fix relies on must not be loosened."""
        self.assertEqual(_build_sources([{'text': 'x', 'metadata': {}}]), [])
        self.assertEqual(
            _build_sources([{'text': 'x', 'metadata': {'document_title': 'T'}}]), [])

    def test_sources_remain_deduplicated_per_document(self):
        chunk = {'text': 'x', 'metadata': {'document_title': 'Panel',
                                           'document_id': 'same',
                                           'record_date': '2026-01-01'}}
        self.assertEqual(len(_build_sources([chunk, chunk, chunk])), 1)

    def test_general_temporal_path_still_carries_citations(self):
        """The no-biomarker timeline path already had document_id; keep it."""
        record = MedicalRecord.objects.create(
            patient=self.user, title='Clinic Note', record_type='diagnosis',
            record_date=date(2026, 5, 20), raw_text='Reviewed in clinic today.',
        )
        DocumentProcessor().process_record(record)

        _context, chunks = TrajectoryService().get_trajectory_context(
            self.user, 'what happened to my health over time', temporal_mode='trend')
        sources = _build_sources(chunks)
        self.assertTrue(sources)
        self.assertEqual(sources[0]['title'], 'Clinic Note')

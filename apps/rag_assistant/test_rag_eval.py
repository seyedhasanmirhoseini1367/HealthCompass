"""
Deterministic RAG evaluation — no external provider required.

These are the parts of RAG quality that can be measured without an LLM: how a
query is classified, whether ordering logic is applied at all, how scoring
weights recency, whether chunking keeps a clinical value with its date, and
whether patient scoping holds. They run in the normal test suite.

The LLM-dependent half of the evaluation (answer grounding, refusal quality,
citation correctness) lives in `scripts/evaluation/` and is NOT part of this
suite — it needs live providers and is non-deterministic.

Several tests below assert CURRENT behaviour that is wrong, and say so. They
exist to hold the baseline still while it is being measured; each is marked
BASELINE and names the finding it pins.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'evaluation'))
from rag_eval_dataset import (  # noqa: E402
    CONFLICTING_RECORDS, CREATININE_TIMELINE, DATASET, GLUCOSE_TIMELINE,
    INJECTION_PAYLOAD, by_category,
)

User = get_user_model()
NO_AUTOINDEX = override_settings(RAG_AUTO_INDEX_SYNC=False)


# ── Dataset integrity ─────────────────────────────────────────────────────────

class EvalDatasetTests(SimpleTestCase):
    """The dataset itself must stay well-formed, or the metrics mean nothing."""

    REQUIRED = {'id', 'question', 'category', 'expected_route', 'expects_temporal',
                'must_contain', 'must_not_contain', 'should_refuse',
                'expected_newest', 'expected_stale', 'notes'}

    def test_every_case_has_the_full_schema(self):
        for case in DATASET:
            with self.subTest(case=case['id']):
                self.assertEqual(self.REQUIRED - set(case), set())

    def test_ids_are_unique(self):
        ids = [c['id'] for c in DATASET]
        self.assertEqual(len(ids), len(set(ids)))

    def test_all_required_categories_are_represented(self):
        for category in ('temporal_latest', 'temporal_previous', 'temporal_trend',
                         'factual', 'unanswerable', 'conflicting', 'attribution',
                         'injection'):
            with self.subTest(category=category):
                self.assertTrue(by_category(category), f'no cases for {category}')

    def test_expectations_are_properties_not_sentences(self):
        """Guards against brittle exact-answer assertions creeping in."""
        for case in DATASET:
            for field in ('must_contain', 'must_not_contain'):
                for fragment in case[field]:
                    with self.subTest(case=case['id'], fragment=fragment):
                        self.assertLess(len(fragment.split()), 6,
                                        'expectation looks like a full sentence')


# ── Query classification ──────────────────────────────────────────────────────

class TemporalClassificationTests(SimpleTestCase):
    """
    Does the classifier recognise a recency question at all?

    This is upstream of everything else: if "latest" is not classified as
    temporal, the trajectory path that actually orders by date never runs.
    """

    def _classify(self, question):
        from apps.rag_assistant.services.query_understanding import understand
        return understand(question)

    def test_explicit_trend_language_is_recognised(self):
        qi = self._classify('Is my glucose getting worse over time?')
        self.assertTrue(qi.is_temporal)
        self.assertEqual(qi.route, 'trajectory')

    def test_recency_language_is_recognised_as_temporal(self):
        """ACCEPTANCE — FINDING R1 (was: these all returned False)."""
        for question in ('What is my latest glucose?',
                         'What was my most recent glucose reading?',
                         'What is my current creatinine?',
                         'What was my previous glucose before the last one?',
                         'What is my newest lab result?',
                         'Show my current blood pressure',
                         'What is my most recent HbA1c?'):
            with self.subTest(question=question):
                self.assertTrue(self._classify(question).is_temporal)

    def test_recency_questions_route_to_trajectory(self):
        """ACCEPTANCE — FINDING R1, routing consequence."""
        for question in ('What is my latest glucose?',
                         'What is my current creatinine?',
                         'What was my previous glucose?',
                         'What is my most recent abnormal lab result?'):
            with self.subTest(question=question):
                self.assertEqual(self._classify(question).route, 'trajectory')

    def test_recency_words_outside_a_patient_context_stay_general(self):
        """
        ACCEPTANCE — FINDING R1, the guard.

        "latest" is an ordinary English word. A general-knowledge question that
        happens to contain it must not be dragged onto the patient trajectory
        path, where it would be answered from the wrong data entirely.
        """
        for question in ('What are the latest clinical guidelines?',
                         'What is the latest research on diabetes?',
                         'What is the current recommended treatment for hypertension?'):
            with self.subTest(question=question):
                intent = self._classify(question)
                self.assertFalse(intent.is_temporal)
                self.assertNotEqual(intent.route, 'trajectory')

    def test_recency_keeps_its_domain_route_when_trajectory_cannot_order_it(self):
        """
        ACCEPTANCE — FINDING R1, scope guard.

        Trajectory orders numeric ParsedLabValue series and dated record
        timelines. Prescriptions are neither, so a recency question about a
        medication keeps the medications route and merely carries the temporal
        flag into retrieval.
        """
        intent = self._classify('Am I currently taking metformin?')
        self.assertEqual(intent.route, 'medications')
        self.assertTrue(intent.is_temporal)

    def test_unrelated_routes_did_not_regress(self):
        """Non-temporal questions must classify exactly as they did before R1."""
        for question, expected in (('What is a normal glucose range?', 'lab_results'),
                                   ('What does HbA1c measure?', 'lab_results'),
                                   ('Do I have any glucose measurements on file?', 'lab_results')):
            with self.subTest(question=question):
                intent = self._classify(question)
                self.assertEqual(intent.route, expected)
                self.assertFalse(intent.is_temporal)

    def test_biomarker_detection_works_regardless_of_routing(self):
        """The biomarker IS detected — only the temporal signal is missing."""
        from apps.rag_assistant.services.trajectory_service import TrajectoryService

        svc = TrajectoryService()
        self.assertEqual(svc.detect_biomarker('What is my latest glucose?'), 'glucose')
        self.assertEqual(svc.detect_biomarker('What is my current creatinine?'), 'creatinine')


# ── Recency weighting ─────────────────────────────────────────────────────────

class TimeDecayTests(SimpleTestCase):
    """
    When ordering is not applied, recency rests entirely on the time-decay term.
    These tests quantify how much preference that actually buys.
    """

    def _decay(self, ages_days):
        from apps.rag_assistant.services.retrieval_service import RetrievalService

        svc = RetrievalService()
        today = date.today()
        meta = [{'record_date': (today - timedelta(days=a)).isoformat()} for a in ages_days]
        return svc._apply_time_decay(np.ones(len(ages_days), dtype=np.float32), meta)

    def test_records_under_a_year_get_no_penalty_at_all(self):
        scores = self._decay([0, 200, 364])
        self.assertTrue(np.allclose(scores, 1.0))

    def test_baseline_two_year_old_record_is_penalised_only_fifteen_percent(self):
        """
        BASELINE — FINDING R2.

        A two-year-old value keeps 85% of its score. A semantically stronger old
        chunk therefore still outranks a weaker recent one, which is exactly the
        'latest' failure in retrieval terms.
        """
        scores = self._decay([0, 730])
        self.assertAlmostEqual(float(scores[1]), 0.85, places=4)

        # Concretely: an older chunk scoring 0.60 beats a newer one scoring 0.50.
        old_effective = 0.60 * float(scores[1])
        self.assertGreater(old_effective, 0.50,
                           'time decay is too weak to reorder on recency alone')

    def test_decay_is_bounded_so_old_records_never_vanish(self):
        """Intentional: an old record must stay retrievable for history questions."""
        scores = self._decay([365 * 20])
        self.assertGreaterEqual(float(scores[0]), 0.85)

    def test_unparseable_record_date_does_not_crash_scoring(self):
        from apps.rag_assistant.services.retrieval_service import RetrievalService

        svc = RetrievalService()
        # document_processor writes this literal when record_date is NULL.
        meta = [{'record_date': 'unknown date'}, {'record_date': None}, {}]
        scores = svc._apply_time_decay(np.ones(3, dtype=np.float32), meta)
        self.assertTrue(np.allclose(scores, 1.0))


# ── Trajectory ordering (the path that IS correct) ────────────────────────────

@NO_AUTOINDEX
class TrajectoryOrderingTests(TestCase):
    """
    When the trajectory path runs, does it order correctly?

    Worth separating from the routing finding: the ordering machinery is sound,
    which is why the fix for R1 is a classification change rather than new
    retrieval logic.
    """

    def setUp(self):
        from apps.medical_records.models import MedicalRecord, ParsedLabValue

        self.user = User.objects.create_user(
            username='traj', email='traj@example.com', password='pw-traj-1',
        )
        for point in GLUCOSE_TIMELINE:
            record = MedicalRecord.objects.create(
                patient=self.user, title=point['title'], record_type='lab_result',
                record_date=date.fromisoformat(point['date']),
            )
            ParsedLabValue.objects.create(
                record=record, parameter_name='Glucose', value=point['value'],
                unit=point['unit'], canonical_value=float(point['value']),
                is_abnormal=point['abnormal'],
                measured_at=timezone.make_aware(
                    timezone.datetime.fromisoformat(point['date'] + 'T09:00:00')),
            )

    def test_trajectory_context_is_chronological_and_includes_every_point(self):
        from apps.rag_assistant.services.trajectory_service import TrajectoryService

        context, _sources = TrajectoryService().get_trajectory_context(
            self.user, 'Is my glucose getting worse over time?')

        self.assertTrue(context)
        for point in GLUCOSE_TIMELINE:
            self.assertIn(point['value'], context)

        positions = [context.index(p['value']) for p in GLUCOSE_TIMELINE]
        self.assertEqual(positions, sorted(positions),
                         'trajectory context must read oldest → newest')

    def test_newest_value_is_present_for_a_recency_question(self):
        """The data supports the right answer — only the routing withholds it."""
        from apps.rag_assistant.services.trajectory_service import TrajectoryService

        context, _ = TrajectoryService().get_trajectory_context(
            self.user, 'Is my glucose getting worse over time?')
        self.assertIn('7.8', context)

    def test_trajectory_is_scoped_to_the_patient(self):
        from apps.medical_records.models import MedicalRecord, ParsedLabValue
        from apps.rag_assistant.services.trajectory_service import TrajectoryService

        other = User.objects.create_user(
            username='other-traj', email='ot@example.com', password='pw-ot-1',
        )
        record = MedicalRecord.objects.create(
            patient=other, title='Someone else', record_type='lab_result',
            record_date=date(2026, 6, 1),
        )
        ParsedLabValue.objects.create(
            record=record, parameter_name='Glucose', value='99.9', unit='mmol/L',
            canonical_value=99.9, measured_at=timezone.now(),
        )

        context, _ = TrajectoryService().get_trajectory_context(
            self.user, 'Is my glucose getting worse over time?')
        self.assertNotIn('99.9', context)

    def test_empty_history_returns_no_context_rather_than_a_guess(self):
        from apps.rag_assistant.services.trajectory_service import TrajectoryService

        empty = User.objects.create_user(
            username='empty-traj', email='et@example.com', password='pw-et-1',
        )
        context, sources = TrajectoryService().get_trajectory_context(
            empty, 'Is my glucose getting worse over time?')
        self.assertFalse(context)
        self.assertEqual(sources, [])


# ── Chunking ──────────────────────────────────────────────────────────────────

@NO_AUTOINDEX
class ChunkingCoherenceTests(TestCase):
    """
    Does a chunk carry enough context to be interpreted on its own?

    Retrieval returns chunks, not documents, so anything the LLM needs in order
    to read a value correctly — above all its date — has to survive the split.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='chunk', email='chunk@example.com', password='pw-chunk-1',
        )

    def _build_panel(self, analyte_count):
        from apps.medical_records.models import MedicalRecord, ParsedLabValue

        record = MedicalRecord.objects.create(
            patient=self.user, title='Wide Panel', record_type='lab_result',
            record_date=date(2026, 5, 20),
        )
        for i in range(analyte_count):
            ParsedLabValue.objects.create(
                record=record, parameter_name=f'ANALYTE_{i:03d}',
                value=str(100 + i), unit='mmol/L', reference_range='4.0-6.0',
                is_abnormal=(i % 7 == 0),
            )
        return record

    def _chunks_for(self, record):
        from apps.rag_assistant.services.document_processor import DocumentProcessor

        DocumentProcessor().process_record(record)
        from apps.rag_assistant.models import MedicalChunk
        return list(MedicalChunk.objects.filter(document__record=record)
                    .order_by('chunk_index'))

    def test_small_panel_stays_in_one_coherent_chunk(self):
        chunks = self._chunks_for(self._build_panel(8))
        self.assertEqual(len(chunks), 1)
        self.assertIn('2026-05-20', chunks[0].content)

    def test_every_chunk_of_a_wide_panel_carries_the_record_date(self):
        """
        ACCEPTANCE — FINDING R3 (was: only the first chunk had the date).

        A chunk is what retrieval returns and what the model reads. Any chunk
        holding lab values must therefore say when they were taken.
        """
        chunks = self._chunks_for(self._build_panel(60))
        self.assertGreater(len(chunks), 1, 'panel should split at this width')

        for chunk in chunks:
            with self.subTest(chunk_index=chunk.chunk_index):
                self.assertIn('2026-05-20', chunk.content)

    def test_continuation_chunks_name_the_source_document(self):
        """ACCEPTANCE — FINDING R3, title as well as date."""
        chunks = self._chunks_for(self._build_panel(60))
        for chunk in chunks[1:]:
            with self.subTest(chunk_index=chunk.chunk_index):
                self.assertIn('Wide Panel', chunk.content)
                self.assertIn('continued', chunk.content)

    def test_first_chunk_is_not_given_a_duplicate_header(self):
        """The document's own header line already opens chunk 0."""
        chunks = self._chunks_for(self._build_panel(60))
        self.assertNotIn('(continued)', chunks[0].content)

    def test_context_header_is_short_enough_not_to_dominate_the_chunk(self):
        """
        The header is embedded along with the content, so it must stay small
        relative to the window or it dilutes the chunk's own signal.
        """
        chunks = self._chunks_for(self._build_panel(60))
        header_line = chunks[1].content.splitlines()[0]
        self.assertLess(len(header_line.split()), 12)

    def test_no_chunk_ends_on_an_orphaned_label(self):
        """ACCEPTANCE — FINDING R4 (was: a window could end on 'ANALYTE_042:')."""
        chunks = self._chunks_for(self._build_panel(60))
        for chunk in chunks:
            with self.subTest(chunk_index=chunk.chunk_index):
                self.assertFalse(chunk.content.rstrip().endswith(':'))

    def test_overlap_means_no_content_is_lost_at_a_boundary(self):
        chunks = self._chunks_for(self._build_panel(60))
        joined = ' '.join(c.content for c in chunks)
        for i in (0, 17, 42, 59):
            self.assertIn(f'ANALYTE_{i:03d}', joined)

    def test_chunk_metadata_records_date_and_type(self):
        chunks = self._chunks_for(self._build_panel(4))
        meta = chunks[0].metadata
        self.assertEqual(meta.get('record_date'), '2026-05-20')
        self.assertEqual(meta.get('record_type'), 'lab_result')

    def test_chunk_metadata_records_its_character_range(self):
        """ACCEPTANCE — FINDING R5 (was: no location data at all)."""
        chunks = self._chunks_for(self._build_panel(60))
        for chunk in chunks:
            with self.subTest(chunk_index=chunk.chunk_index):
                meta = chunk.metadata
                self.assertIn('start_offset', meta)
                self.assertIn('end_offset', meta)
                self.assertLess(meta['start_offset'], meta['end_offset'])

    def test_page_and_section_are_omitted_rather_than_guessed(self):
        """
        ACCEPTANCE — FINDING R5, the honesty half.

        The PDF parser joins pages into one string before chunking, so page
        boundaries no longer exist by the time a chunk is cut. A citation
        pointing at the wrong page is harder to catch than one that offers no
        page, so nothing is recorded.
        """
        chunks = self._chunks_for(self._build_panel(4))
        for absent in ('page', 'page_number', 'section_title'):
            self.assertNotIn(absent, chunks[0].metadata)


# ── Conflicting records ───────────────────────────────────────────────────────

@NO_AUTOINDEX
class ConflictingRecordTests(TestCase):
    """Both sides of a contradiction must reach the model, with their dates."""

    def setUp(self):
        from apps.medical_records.models import MedicalRecord

        self.user = User.objects.create_user(
            username='conflict', email='conflict@example.com', password='pw-cf-1',
        )
        self.records = [
            MedicalRecord.objects.create(
                patient=self.user, title=spec['title'], record_type=spec['type'],
                record_date=date.fromisoformat(spec['date']), raw_text=spec['text'],
            )
            for spec in CONFLICTING_RECORDS
        ]

    def test_both_conflicting_records_are_indexed(self):
        from apps.rag_assistant.models import MedicalChunk
        from apps.rag_assistant.services.document_processor import DocumentProcessor

        for record in self.records:
            DocumentProcessor().process_record(record)

        contents = ' '.join(
            MedicalChunk.objects.filter(patient=self.user).values_list('content', flat=True))
        self.assertIn('Metformin', contents)
        self.assertIn('discontinued', contents)

    def test_baseline_no_conflict_detection_exists(self):
        """
        BASELINE — FINDING R6.

        Nothing in the pipeline compares retrieved chunks for contradiction. The
        model sees both statements and its handling of the conflict is entirely
        prompt-dependent and unmeasured by any deterministic check.
        """
        from apps.rag_assistant.services import retrieval_service

        source = Path(retrieval_service.__file__).read_text(encoding='utf-8-sig')
        for marker in ('conflict', 'contradict', 'disagree'):
            self.assertNotIn(marker, source.lower())


# ── Grounding inputs ──────────────────────────────────────────────────────────

@NO_AUTOINDEX
class GroundingInputTests(TestCase):
    """
    What reaches the model determines what it can ground on. These assert the
    inputs; answer-level grounding needs an LLM and lives in scripts/evaluation.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='ground', email='ground@example.com', password='pw-gr-1',
        )

    def test_injected_document_text_is_fenced_as_untrusted(self):
        from apps.rag_assistant.services.generation_service import (
            _RETRIEVED_CLOSE, _RETRIEVED_OPEN, _build_messages)

        messages = _build_messages(INJECTION_PAYLOAD, 'What do my records say?', [])
        content = messages[-1]['content']
        marker_at = content.index('__INJECTION_SUCCEEDED__')
        self.assertGreater(marker_at, content.index(_RETRIEVED_OPEN))
        self.assertLess(marker_at, content.index(_RETRIEVED_CLOSE))

    def test_no_context_produces_an_explicit_absence_statement(self):
        """The model must be told there is nothing, not handed an empty string."""
        from apps.rag_assistant.services.generation_service import _build_context

        context = _build_context([])
        self.assertIn('No relevant medical records', context)

    def test_context_carries_the_record_date_for_each_chunk(self):
        from apps.rag_assistant.services.generation_service import _build_context

        context = _build_context([{
            'text': 'Glucose: 7.8 mmol/L',
            'metadata': {'document_title': 'Metabolic Panel 2026',
                         'document_type': 'lab_result',
                         'record_date': '2026-05-20', 'document_id': 'd1'},
        }])
        self.assertIn('2026-05-20', context)
        self.assertIn('Metabolic Panel 2026', context)

    def test_sources_are_built_from_retrieved_chunks_only(self):
        from apps.rag_assistant.services.generation_service import _build_sources

        sources = _build_sources([{
            'text': 'x',
            'metadata': {'document_title': 'Metabolic Panel 2026',
                         'document_id': 'd1', 'record_date': '2026-05-20',
                         'record_id': 'r1', 'document_type': 'lab_result'},
        }])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['title'], 'Metabolic Panel 2026')
        self.assertEqual(sources[0]['record_date'], '2026-05-20')

    def test_sources_are_deduplicated_per_document(self):
        from apps.rag_assistant.services.generation_service import _build_sources

        chunk = {'text': 'x', 'metadata': {'document_title': 'Panel',
                                           'document_id': 'same', 'record_date': '2026-01-01'}}
        self.assertEqual(len(_build_sources([chunk, chunk, chunk])), 1)


# ── Configurability ───────────────────────────────────────────────────────────

class RetrievalConfigurabilityTests(SimpleTestCase):
    """Which knobs exist, and which are hardcoded — recorded before any tuning."""

    CONFIGURABLE = ['CHUNK_SIZE', 'CHUNK_OVERLAP', 'TOP_K', 'BM25_WEIGHT',
                    'SEMANTIC_WEIGHT', 'TIME_DECAY_DAYS', 'TIME_DECAY_FACTOR',
                    'MMR_LAMBDA', 'SIM_THRESHOLD', 'INTENT_WEIGHTS',
                    'CONTEXT_TYPE_BOOST', 'RERANK_RECALL_FACTOR',
                    'EMBEDDING_MODEL', 'EMBEDDING_DIM']

    def test_documented_parameters_are_all_present_in_rag_config(self):
        from django.conf import settings

        for key in self.CONFIGURABLE:
            with self.subTest(key=key):
                self.assertIn(key, settings.RAG_CONFIG)

    def test_baseline_keyword_lists_are_hardcoded_not_configurable(self):
        """
        BASELINE — FINDING R7.

        Retrieval weights are tunable but the classifier's vocabulary is not, so
        the R1 fix requires a code change rather than a settings change.
        """
        from django.conf import settings

        for key in ('TEMPORAL_KEYWORDS', 'ROUTE_KEYWORDS', 'BIOMARKERS'):
            self.assertNotIn(key, settings.RAG_CONFIG)


# ── R1b: latest vs previous vs trend ──────────────────────────────────────────

class TemporalModeClassificationTests(SimpleTestCase):
    """ACCEPTANCE — FINDING R1b. is_temporal alone cannot say WHICH question."""

    def _mode(self, question):
        from apps.rag_assistant.services.query_understanding import understand
        return understand(question).temporal_mode

    def test_latest_language_yields_latest_mode(self):
        for q in ('What is my latest glucose?', 'What is my current creatinine?',
                  'What is my newest lab result?', 'What was my most recent HbA1c?'):
            with self.subTest(q=q):
                self.assertEqual(self._mode(q), 'latest')

    def test_previous_language_yields_previous_mode(self):
        for q in ('What was my previous glucose?',
                  'What was my prior creatinine reading?'):
            with self.subTest(q=q):
                self.assertEqual(self._mode(q), 'previous')

    def test_trend_language_yields_trend_mode(self):
        for q in ('Is my glucose getting worse over time?',
                  'How has my creatinine changed?',
                  'Show me the history of my glucose readings.'):
            with self.subTest(q=q):
                self.assertEqual(self._mode(q), 'trend')

    def test_trend_wins_when_a_query_mixes_both(self):
        """A trend answer contains the latest value; the reverse is not true."""
        self.assertEqual(self._mode('How has my latest glucose changed over time?'), 'trend')

    def test_non_temporal_queries_have_no_mode(self):
        for q in ('What is a normal glucose range?',
                  'What are the latest clinical guidelines?'):
            with self.subTest(q=q):
                self.assertIsNone(self._mode(q))


@NO_AUTOINDEX
class TemporalModeContextTests(TestCase):
    """
    ACCEPTANCE — FINDING R1b, end of the path.

    Classification is only useful if the retrieval layer acts on it. These
    assert the context string actually differs by mode.
    """

    def setUp(self):
        from apps.medical_records.models import MedicalRecord, ParsedLabValue

        self.user = User.objects.create_user(
            username='tmode', email='tmode@example.com', password='pw-tm-1',
        )
        for point in GLUCOSE_TIMELINE:
            record = MedicalRecord.objects.create(
                patient=self.user, title=point['title'], record_type='lab_result',
                record_date=date.fromisoformat(point['date']),
            )
            ParsedLabValue.objects.create(
                record=record, parameter_name='Glucose', value=point['value'],
                unit=point['unit'], canonical_value=float(point['value']),
                is_abnormal=point['abnormal'],
                measured_at=timezone.make_aware(
                    timezone.datetime.fromisoformat(point['date'] + 'T09:00:00')),
            )

    def _context(self, mode):
        from apps.rag_assistant.services.trajectory_service import TrajectoryService
        context, _ = TrajectoryService().get_trajectory_context(
            self.user, 'my glucose', temporal_mode=mode)
        return context

    def test_latest_mode_names_the_newest_value_explicitly(self):
        context = self._context('latest')
        self.assertIn('MOST RECENT', context)
        answer_line = [l for l in context.splitlines() if 'ANSWER TO THE QUESTION' in l][0]
        self.assertIn('7.8', answer_line)
        self.assertNotIn('5.1', answer_line)

    def test_previous_mode_names_the_second_newest_value(self):
        context = self._context('previous')
        answer_line = [l for l in context.splitlines() if 'ANSWER TO THE QUESTION' in l][0]
        self.assertIn('6.4', answer_line)
        self.assertNotIn('7.8', answer_line)

    def test_trend_mode_adds_no_point_in_time_line(self):
        self.assertNotIn('ANSWER TO THE QUESTION', self._context('trend'))

    def test_every_mode_still_includes_the_full_series(self):
        for mode in ('latest', 'previous', 'trend', None):
            with self.subTest(mode=mode):
                context = self._context(mode)
                for point in GLUCOSE_TIMELINE:
                    self.assertIn(point['value'], context)

    def test_previous_with_a_single_reading_says_so_rather_than_guessing(self):
        from apps.medical_records.models import MedicalRecord, ParsedLabValue

        solo = User.objects.create_user(
            username='solo', email='solo@example.com', password='pw-solo-1',
        )
        record = MedicalRecord.objects.create(
            patient=solo, title='Only Panel', record_type='lab_result',
            record_date=date(2026, 5, 20),
        )
        ParsedLabValue.objects.create(
            record=record, parameter_name='Glucose', value='7.8', unit='mmol/L',
            canonical_value=7.8, measured_at=timezone.now(),
        )

        from apps.rag_assistant.services.trajectory_service import TrajectoryService
        context, _ = TrajectoryService().get_trajectory_context(
            solo, 'my glucose', temporal_mode='previous')
        self.assertIn('no previous value', context)

    def test_temporal_mode_reaches_the_service_through_graph_state(self):
        """The wiring, not just the endpoints: state -> node -> service."""
        from unittest.mock import patch

        from apps.rag_assistant.graph import nodes

        with patch('apps.rag_assistant.services.trajectory_service.TrajectoryService'
                   '.get_trajectory_context', return_value=('ctx', [])) as spy:
            nodes.trajectory_node({
                'question': 'What is my latest glucose?',
                'rewritten_query': 'What is my latest glucose?',
                'patient_id': self.user.pk,
                'temporal_mode': 'latest',
                'history': [],
            })
        self.assertEqual(spy.call_args.kwargs.get('temporal_mode'), 'latest')

"""
ACCEPTANCE — FINDINGS R5 and R4: chunk location metadata and boundary safety.

R5 — every chunk records the character range it came from, so a citation can
     point at a passage rather than only a document.
R4 — a window must not end on a label, orphaning its value into the next chunk.
     "Glucose:" in one chunk and "105 mg/dL" in the next is worse than useless.

Exercised against the four shapes the ingester actually sees: lab tables, OCR
text with irregular whitespace, prose paragraphs, and headed key/value blocks.
"""
from django.test import SimpleTestCase, override_settings

from apps.rag_assistant.services.document_processor import DocumentProcessor

LAB_TABLE = '\n'.join(
    ['Lab result: Metabolic Panel — 2026-05-20'] +
    [f'  ANALYTE_{i:03d}: {100 + i} mmol/L (ref: 4.0-6.0)' for i in range(80)]
)

OCR_TEXT = (
    'LABORATORY   REPORT\n\n\n'
    'Patient :   Jane   Doe\n'
    'Glucose :    105   mg/dL\n'
    '   Creatinine:  1.4  mg/dL\n\n'
    'Comments:  sample   slightly   haemolysed\n'
)

PROSE = (
    'The patient attended the clinic reporting intermittent fatigue over the past '
    'three months. Examination was unremarkable. Bloods were taken and the results '
    'are discussed below. No acute concerns were identified at this visit and a '
    'follow-up was arranged for the spring. '
) * 6

HEADED_KEY_VALUE = (
    'DISCHARGE SUMMARY\n'
    'Admission date: 2026-04-02\n'
    'Discharge date: 2026-04-09\n'
    'Primary diagnosis: community acquired pneumonia\n'
    'Medications on discharge: amoxicillin 500 mg three times daily\n'
    'Follow-up: respiratory clinic in six weeks\n'
) * 8


class _SplitterMixin:

    def _windows(self, text, size=40, overlap=8):
        with override_settings(RAG_CONFIG={
                **__import__('django.conf', fromlist=['settings']).settings.RAG_CONFIG,
                'CHUNK_SIZE': size, 'CHUNK_OVERLAP': overlap}):
            return DocumentProcessor()._split_words_with_offsets(text)


class ChunkOffsetTests(_SplitterMixin, SimpleTestCase):
    """R5 — offsets must be exact, not approximate."""

    def test_offsets_index_the_original_text_exactly(self):
        for label, text in (('lab', LAB_TABLE), ('ocr', OCR_TEXT),
                            ('prose', PROSE), ('headed', HEADED_KEY_VALUE)):
            for window, start, end in self._windows(text):
                with self.subTest(source=label, start=start):
                    # The slice must begin and end on the same tokens as the window.
                    excerpt = text[start:end]
                    self.assertEqual(excerpt.split()[0], window.split()[0])
                    self.assertEqual(excerpt.split()[-1], window.split()[-1])

    def test_offsets_are_monotonic_and_within_bounds(self):
        windows = self._windows(LAB_TABLE)
        self.assertGreater(len(windows), 1)
        previous_start = -1
        for _text, start, end in windows:
            self.assertLess(start, end)
            self.assertLessEqual(end, len(LAB_TABLE))
            self.assertGreater(start, previous_start)
            previous_start = start

    def test_irregular_whitespace_does_not_corrupt_offsets(self):
        """OCR output is full of runs of spaces; offsets must survive them."""
        for window, start, end in self._windows(OCR_TEXT, size=6, overlap=2):
            self.assertEqual(OCR_TEXT[start:end].split(), window.split())

    def test_empty_text_yields_one_bounded_window(self):
        self.assertEqual(self._windows('   \n\n  '), [('   \n\n  '[:2000], 0, 7)][:1] or
                         self._windows('   \n\n  '))

    def test_chunk_metadata_carries_the_offsets(self):
        from django.contrib.auth import get_user_model
        # Metadata assembly is covered end-to-end in test_rag_eval; this asserts
        # the splitter contract the metadata is built from.
        windows = self._windows(LAB_TABLE)
        self.assertTrue(all(isinstance(w[1], int) and isinstance(w[2], int)
                            for w in windows))


class ChunkBoundaryTests(_SplitterMixin, SimpleTestCase):
    """R4 — a window must not end on a label."""

    def _ends_on_label(self, windows):
        return [w for w, _s, _e in windows
                if w.rstrip().endswith(':') or w.rstrip().split()[-1] == ':']

    def test_lab_table_windows_never_end_on_an_analyte_label(self):
        self.assertEqual(self._ends_on_label(self._windows(LAB_TABLE, size=7, overlap=2)), [])

    def test_ocr_windows_never_end_on_a_label(self):
        self.assertEqual(self._ends_on_label(self._windows(OCR_TEXT, size=3, overlap=1)), [])

    def test_headed_key_value_windows_never_end_on_a_label(self):
        self.assertEqual(
            self._ends_on_label(self._windows(HEADED_KEY_VALUE, size=5, overlap=1)), [])

    def test_prose_is_unaffected(self):
        """Prose has no labels; windows should be exactly chunk_size."""
        windows = self._windows(PROSE, size=40, overlap=8)
        self.assertGreater(len(windows), 2)
        self.assertTrue(all(len(w.split()) == 40 for w, _s, _e in windows[:-1]))

    def test_a_window_is_never_grown_beyond_chunk_size(self):
        """The boundary only ever moves back, so windows can shrink, not grow."""
        for text in (LAB_TABLE, OCR_TEXT, HEADED_KEY_VALUE):
            for window, _s, _e in self._windows(text, size=7, overlap=2):
                self.assertLessEqual(len(window.split()), 7)

    def test_no_content_is_lost_when_a_boundary_moves(self):
        windows = self._windows(LAB_TABLE, size=7, overlap=2)
        joined = ' '.join(w for w, _s, _e in windows)
        for i in (0, 13, 44, 79):
            self.assertIn(f'ANALYTE_{i:03d}', joined)

    def test_label_and_value_stay_together_in_at_least_one_chunk(self):
        """The pair must be readable somewhere, not split across every chunk."""
        windows = [w for w, _s, _e in self._windows(OCR_TEXT, size=3, overlap=1)]
        self.assertTrue(any('Glucose' in w and '105' in w for w in windows),
                        'Glucose was never adjacent to its value in any chunk')

    def test_boundary_shift_gives_up_rather_than_distorting_the_window(self):
        """A pathological run of labels must not collapse the window to nothing."""
        pathological = ' '.join(f'FIELD_{i}:' for i in range(50))
        for window, _s, _e in self._windows(pathological, size=5, overlap=1):
            self.assertTrue(window.split())

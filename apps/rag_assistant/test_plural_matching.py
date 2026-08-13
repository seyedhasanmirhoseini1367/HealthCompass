"""
REGRESSION — M1: singular/plural keyword matching.

Matching was exact (`\\bplatelet\\b`), so it missed "platelets". Because
`platelets` is the *canonical* biomarker name, a question like "what are my
platelets?" detected no biomarker and never reached the trajectory path.
Confirmed on all 15 patients of the MIMIC evaluation corpus before the fix.

The same gap affected `result`/`results` and `medication`/`medications` in the
route keyword tables.

These tests pin both halves: the plurals that must now match, and the
near-misses that must still NOT match — a substring fix would have passed the
first set and failed the second.
"""
from django.test import SimpleTestCase

from apps.rag_assistant.services.text_match import matches, word_forms


class WordFormTests(SimpleTestCase):
    """The inflection rules themselves."""

    def test_regular_plural(self):
        self.assertEqual(word_forms('platelet'), {'platelet', 'platelets'})

    def test_is_to_es(self):
        self.assertIn('diagnoses', word_forms('diagnosis'))

    def test_consonant_y_to_ies(self):
        self.assertIn('therapies', word_forms('therapy'))

    def test_sibilant_takes_es(self):
        self.assertIn('reflexes', word_forms('reflex'))

    def test_plural_alias_also_yields_the_singular(self):
        self.assertIn('platelet', word_forms('platelets'))

    def test_short_and_punctuated_aliases_are_left_alone(self):
        """'k+' and 'bp' must not sprout inflections that could collide."""
        self.assertEqual(word_forms('k+'), {'k+'})
        self.assertEqual(word_forms('na+'), {'na+'})

    def test_empty_input(self):
        self.assertEqual(word_forms(''), set())
        self.assertEqual(word_forms(None), set())


class PluralMatchingTests(SimpleTestCase):
    """ACCEPTANCE — M1."""

    def test_platelet_matches_platelets(self):
        self.assertTrue(matches('platelet', 'my platelets are low'))

    def test_result_matches_results(self):
        self.assertTrue(matches('result', 'were any of my december results abnormal'))

    def test_medication_matches_medications(self):
        self.assertTrue(matches('medication', 'what are my current medications'))

    def test_multiword_alias_inflects_only_the_last_word(self):
        self.assertTrue(matches('platelet count', 'my platelet counts'))
        self.assertFalse(matches('platelet count', 'my platelets count'))

    def test_singular_still_matches(self):
        for phrase, text in (('platelet', 'my platelet count'),
                             ('result', 'my lab result'),
                             ('medication', 'my medication list')):
            with self.subTest(phrase=phrase):
                self.assertTrue(matches(phrase, text))


class FalsePositiveGuardTests(SimpleTestCase):
    """
    The reason this is not a substring match.

    Each of these passes trivially under `alias in text` and must not match.
    """

    def test_alias_inside_a_longer_word_does_not_match(self):
        self.assertFalse(matches('bp', 'my bpm is 70'))
        self.assertFalse(matches('a1c', 'my ha1c value'))
        self.assertFalse(matches('platelet', 'plateletpheresis was performed'))
        self.assertFalse(matches('creat', 'please recreate the report'))

    def test_punctuated_aliases_still_match_exactly(self):
        self.assertTrue(matches('na+', 'serum na+ level'))
        self.assertTrue(matches('k+', 'potassium k+ level'))

    def test_unrelated_text_does_not_match(self):
        self.assertFalse(matches('platelet', 'my glucose is high'))

    def test_empty_arguments_are_safe(self):
        self.assertFalse(matches('', 'anything'))
        self.assertFalse(matches('platelet', ''))


class BiomarkerDetectionTests(SimpleTestCase):
    """The user-visible symptom M1 was reported as."""

    def _detect(self, text):
        from apps.rag_assistant.services.trajectory_service import TrajectoryService
        return TrajectoryService().detect_biomarker(text)

    def test_platelets_is_detected(self):
        """Was None before the fix — the canonical name missed its own alias."""
        self.assertEqual(self._detect('what are my platelets'), 'platelets')

    def test_platelet_variants_all_resolve(self):
        for text in ('my platelet', 'my platelets', 'my platelet count',
                     'my platelet counts', 'my plt', 'my thrombocytes'):
            with self.subTest(text=text):
                self.assertEqual(self._detect(text), 'platelets')

    def test_other_biomarkers_did_not_regress(self):
        for text, expected in (('my glucose', 'glucose'),
                               ('my creatinine', 'creatinine'),
                               ('my hba1c', 'hba1c'),
                               ('my egfr', 'egfr'),
                               ('my sodium', 'sodium')):
            with self.subTest(text=text):
                self.assertEqual(self._detect(text), expected)

    def test_non_biomarker_text_still_returns_none(self):
        for text in ('what are the latest guidelines', 'plateletpheresis procedure'):
            with self.subTest(text=text):
                self.assertIsNone(self._detect(text))


class RoutingRegressionTests(SimpleTestCase):
    """Routing consequences of the fix — improvements and non-regressions."""

    def _intent(self, question):
        from apps.rag_assistant.services.query_understanding import understand
        return understand(question)

    def test_plural_results_now_routes_to_lab_results(self):
        """Was 'general' — `\\bresult\\b` did not match "results"."""
        self.assertEqual(
            self._intent('Were any of my December results flagged as critical?').route,
            'lab_results')

    def test_plural_medications_now_routes_to_medications(self):
        self.assertEqual(self._intent('What are my current medications?').route,
                         'medications')

    def test_temporal_routing_did_not_regress(self):
        for question, route in (('What is my latest glucose?', 'trajectory'),
                                ('Is my glucose getting worse over time?', 'trajectory'),
                                ('What was my previous glucose?', 'trajectory')):
            with self.subTest(question=question):
                self.assertEqual(self._intent(question).route, route)

    def test_general_knowledge_did_not_regress(self):
        intent = self._intent('What are the latest clinical guidelines?')
        self.assertFalse(intent.is_temporal)
        self.assertNotEqual(intent.route, 'trajectory')

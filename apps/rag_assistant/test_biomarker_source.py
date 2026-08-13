"""
REGRESSION — M2: one biomarker definition, shared by both consumers.

The vocabulary existed twice — `trajectory_service._BIOMARKERS` (16 entries) and
`query_understanding._BIOMARKER_ALIASES` (10). They drifted, so the trajectory
service could recognise a biomarker the classifier could not, and recency
questions about blood_pressure, heart_rate, platelets, urea, wbc and weight were
routed as though no biomarker had been named.

The second table is gone rather than topped up: `services/biomarkers.py` is the
only definition, and both modules import it. These tests pin that there is one
table, that every entry in it survives the whole routing path, and that the M1
inflection protections still hold.
"""
from django.test import SimpleTestCase

from apps.rag_assistant.services.biomarkers import (
    BIOMARKERS, CANONICAL_NAMES, aliases_for, detect,
)
from apps.rag_assistant.services.query_understanding import (
    _detect_biomarker, understand,
)
from apps.rag_assistant.services.trajectory_service import (
    _BIOMARKERS as TRAJECTORY_TABLE, TrajectoryService,
)

#: One representative alias per biomarker, used to drive the full routing path.
PROBES = {canonical: aliases[0] for canonical, aliases in BIOMARKERS.items()}

#: The six that the classifier could not name before this fix.
PREVIOUSLY_MISSING = ('blood_pressure', 'heart_rate', 'platelets',
                      'urea', 'wbc', 'weight')


class SingleSourceOfTruthTests(SimpleTestCase):
    """There must be exactly one table, and both consumers must use it."""

    def test_trajectory_table_is_the_shared_table(self):
        self.assertIs(TRAJECTORY_TABLE, BIOMARKERS)

    def test_query_understanding_has_no_private_table(self):
        """The duplicate is deleted, not merely synchronised."""
        from apps.rag_assistant.services import query_understanding

        self.assertFalse(hasattr(query_understanding, '_BIOMARKER_ALIASES'))

    def test_both_detectors_are_the_same_function_behaviourally(self):
        service = TrajectoryService()
        for canonical, probe in PROBES.items():
            with self.subTest(biomarker=canonical):
                text = f'my {probe}'
                self.assertEqual(service.detect_biomarker(text), canonical)
                self.assertEqual(_detect_biomarker(text), canonical)
                self.assertEqual(detect(text), canonical)

    def test_no_module_defines_a_second_biomarker_table(self):
        """Guards against a new copy appearing somewhere else later."""
        import pathlib

        services = pathlib.Path(
            __file__).resolve().parent / 'services'
        offenders = []
        for path in services.glob('*.py'):
            if path.name == 'biomarkers.py':
                continue
            text = path.read_text(encoding='utf-8-sig', errors='strict')
            for marker in ('_BIOMARKER_ALIASES = {', '_BIOMARKERS: Dict[str, List[str]] = {'):
                if marker in text:
                    offenders.append(f'{path.name}: {marker}')
        self.assertEqual(offenders, [], f'a second biomarker table reappeared: {offenders}')


class CoverageTests(SimpleTestCase):
    """Every biomarker in the table must be reachable end to end."""

    def test_all_sixteen_are_present(self):
        self.assertEqual(len(BIOMARKERS), 16)
        for canonical in PREVIOUSLY_MISSING:
            self.assertIn(canonical, CANONICAL_NAMES)

    def test_every_alias_resolves_to_its_own_canonical_name(self):
        """
        No alias may be shadowed by an earlier entry. Ordering in the table is
        load-bearing — a short ambiguous alias placed first would silently
        capture queries meant for another biomarker.
        """
        for canonical, aliases in BIOMARKERS.items():
            for alias in aliases:
                with self.subTest(biomarker=canonical, alias=alias):
                    self.assertEqual(detect(f'my {alias}'), canonical)

    def test_aliases_for_returns_the_shared_list(self):
        self.assertEqual(aliases_for('platelets'), BIOMARKERS['platelets'])
        self.assertEqual(aliases_for('not_a_biomarker'), [])


class RoutingCoverageTests(SimpleTestCase):
    """
    ACCEPTANCE — M2. All 16 biomarkers, all three temporal modes.

    The point of the fix: `_route_kw()` consults `QueryIntent.biomarker` when
    deciding whether a recency question takes the trajectory path, so a
    biomarker the classifier cannot name is a biomarker whose recency questions
    are misrouted.
    """

    def _intent(self, question):
        return understand(question)

    def test_query_intent_biomarker_is_populated_for_all_sixteen(self):
        for canonical, probe in PROBES.items():
            with self.subTest(biomarker=canonical):
                self.assertEqual(
                    self._intent(f'what is my latest {probe}').biomarker, canonical)

    def test_latest_questions_reach_trajectory_for_all_sixteen(self):
        for canonical, probe in PROBES.items():
            with self.subTest(biomarker=canonical):
                intent = self._intent(f'what is my latest {probe}')
                self.assertEqual(intent.route, 'trajectory')
                self.assertEqual(intent.temporal_mode, 'latest')

    def test_previous_questions_reach_trajectory_for_all_sixteen(self):
        for canonical, probe in PROBES.items():
            with self.subTest(biomarker=canonical):
                intent = self._intent(f'what was my previous {probe}')
                self.assertEqual(intent.route, 'trajectory')
                self.assertEqual(intent.temporal_mode, 'previous')

    def test_trend_questions_reach_trajectory_for_all_sixteen(self):
        for canonical, probe in PROBES.items():
            with self.subTest(biomarker=canonical):
                intent = self._intent(f'is my {probe} getting worse over time')
                self.assertEqual(intent.route, 'trajectory')
                self.assertEqual(intent.temporal_mode, 'trend')

    def test_the_six_previously_missing_biomarkers_specifically(self):
        """Named explicitly so a regression on these is unmistakable."""
        for canonical in PREVIOUSLY_MISSING:
            probe = PROBES[canonical]
            with self.subTest(biomarker=canonical):
                intent = self._intent(f'what is my latest {probe}')
                self.assertEqual(intent.biomarker, canonical)
                self.assertEqual(intent.route, 'trajectory')


class InflectionStillHoldsTests(SimpleTestCase):
    """M1's protections must survive the M2 refactor."""

    def test_plurals_still_match(self):
        self.assertEqual(detect('my platelets'), 'platelets')
        self.assertEqual(detect('my thrombocytes'), 'platelets')

    def test_false_positives_still_rejected(self):
        self.assertIsNone(detect('plateletpheresis was performed'))
        self.assertIsNone(detect('please recreate the report'))

    def test_bpm_resolves_to_heart_rate_not_blood_pressure(self):
        """
        'bp' is a blood_pressure alias and 'bpm' a heart_rate alias. The
        boundary guard is what keeps 'bp' out of 'bpm'; without it,
        blood_pressure would capture the query first.
        """
        self.assertEqual(detect('my bpm is 70'), 'heart_rate')
        self.assertEqual(detect('my bp is 120/80'), 'blood_pressure')

    def test_punctuated_aliases_still_match(self):
        self.assertEqual(detect('serum na+ level'), 'sodium')
        self.assertEqual(detect('serum k+ level'), 'potassium')

    def test_general_knowledge_questions_name_no_biomarker_falsely(self):
        intent = understand('What are the latest clinical guidelines?')
        self.assertFalse(intent.is_temporal)
        self.assertNotEqual(intent.route, 'trajectory')

"""
A chart that was asked for and not drawn must say so.

The reported bug: a patient asked for a chart of their fasting glucose and got a
prose list, "Here is your chart:" twice, a "(Figure: …)" placeholder — and no
chart.

Two causes, both real:

  1. The system prompt told the model that "the platform renders one
     automatically" and to "never say you cannot show charts". The platform does
     no such thing unconditionally — it needs at least two structured
     ParsedLabValue readings — so the instruction made the model assert
     something false whenever they were absent.

  2. When no chart could be built, the pipeline emitted nothing at all. Silence
     plus a promise is indistinguishable from a broken feature.

The model no longer announces charts, and the absence is now explained.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.medical_records.models import MedicalRecord, ParsedLabValue
from apps.rag_assistant.graph.graph import _chart_unavailable_reason
from apps.rag_assistant.services.generation_service import SYSTEM_PROMPT
from apps.rag_assistant.services.trajectory_service import TrajectoryService

User = get_user_model()


class PromptTests(TestCase):
    """The model must not promise what the platform may not deliver."""

    def test_the_prompt_does_not_tell_the_model_to_announce_a_chart(self):
        """ACCEPTANCE — this instruction is what produced the false claim."""
        lowered = SYSTEM_PROMPT.lower()

        self.assertNotIn('here is your chart', lowered)
        self.assertNotIn('never say you cannot show charts', lowered)

    def test_the_prompt_forbids_placeholders(self):
        """"(Figure: Fasting Glucose Levels Over Time)" was in the answer."""
        self.assertIn('(Figure:', SYSTEM_PROMPT)
        self.assertIn('do not', SYSTEM_PROMPT.lower())

    def test_the_prompt_explains_that_the_model_cannot_know(self):
        self.assertIn('do not know whether', SYSTEM_PROMPT.lower())


class ReasonTests(TestCase):
    """What the patient is told instead."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'ch_patient', email='ch@test.invalid', password='pw', role='patient')
        self.svc = TrajectoryService()
        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result',
            record_date=date(2026, 1, 1))

    def _lab(self, name, value, days_ago=0):
        return ParsedLabValue.objects.create(
            record=self.record, patient=self.patient, parameter_name=name,
            value=str(value), unit='mmol/L',
            measured_at=date.today() - timedelta(days=days_ago))

    def test_no_structured_values_at_all_is_explained(self):
        """
        ACCEPTANCE — the reported case. The patient had readings quoted in the
        answer (from document text) and zero ParsedLabValue rows.
        """
        reason = _chart_unavailable_reason(
            self.svc, self.patient, 'show me a chart of my fasting glucose')

        self.assertIn('structured measurements', reason)
        self.assertIn('No chart', reason)

    def test_values_exist_but_not_for_this_biomarker(self):
        self._lab('Creatinine', 90)

        reason = _chart_unavailable_reason(
            self.svc, self.patient, 'chart my glucose')

        self.assertIn('glucose', reason)
        self.assertIn('none it recognised', reason)

    def test_a_single_reading_is_not_a_trend(self):
        """The distinction worth drawing: one more test would fix this one."""
        self._lab('Glucose', 5.4)

        reason = _chart_unavailable_reason(
            self.svc, self.patient, 'chart my glucose')

        self.assertIn('at least two', reason)

    def test_the_reason_makes_no_clinical_claim(self):
        """
        It describes what the application holds, never what it means. "You have
        no glucose problem" would be a diagnosis drawn from missing data.
        """
        self._lab('Creatinine', 90)
        reason = _chart_unavailable_reason(
            self.svc, self.patient, 'chart my glucose').lower()

        for word in ('normal', 'healthy', 'abnormal', 'fine', 'concern',
                     'diagnos'):
            self.assertNotIn(word, reason)

    def test_an_unknown_biomarker_still_gets_an_answer(self):
        """No crash, and no empty string, for a request naming nothing known."""
        self._lab('Creatinine', 90)

        reason = _chart_unavailable_reason(
            self.svc, self.patient, 'draw me a diagram')

        self.assertTrue(reason)
        self.assertIn('No chart', reason)


class StreamTests(TestCase):
    """The event has to actually reach the browser."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'cs_patient', email='cs@test.invalid', password='pw', role='patient')

    def test_a_chart_request_with_no_data_emits_chart_unavailable(self):
        """ACCEPTANCE — previously this emitted nothing whatsoever."""
        import json
        from unittest.mock import patch

        from apps.rag_assistant.graph import graph as graph_mod

        events = []
        with patch('apps.rag_assistant.services.generation_service.generate_streaming',
                          return_value=iter(['ok'])), \
             patch.object(graph_mod, 'health_graph_routing') as routing:
            routing.invoke.return_value = {
                'chunks': [], 'general_chunks': [], 'route': 'lab_results',
                'display_mode': 'personal', 'answer': '', 'rules_fired': [],
                'retrieval_failed': False,
            }
            for raw in graph_mod.stream_graph(
                    patient=self.patient, query='show me a chart of my glucose',
                    history=[]):
                if raw.startswith('data: '):
                    try:
                        events.append(json.loads(raw[6:].strip()))
                    except ValueError:
                        pass

        kinds = [e.get('type') for e in events]
        self.assertIn('chart_unavailable', kinds)

        note = next(e for e in events if e.get('type') == 'chart_unavailable')
        self.assertTrue(note.get('reason'))

    def test_a_question_that_is_not_a_chart_request_stays_silent(self):
        """No unsolicited notes about charts nobody asked for."""
        import json
        from unittest.mock import patch

        from apps.rag_assistant.graph import graph as graph_mod

        kinds = []
        with patch('apps.rag_assistant.services.generation_service.generate_streaming',
                          return_value=iter(['ok'])), \
             patch.object(graph_mod, 'health_graph_routing') as routing:
            routing.invoke.return_value = {
                'chunks': [], 'general_chunks': [], 'route': 'lab_results',
                'display_mode': 'personal', 'answer': '', 'rules_fired': [],
                'retrieval_failed': False,
            }
            for raw in graph_mod.stream_graph(
                    patient=self.patient, query='what is my creatinine?',
                    history=[]):
                if raw.startswith('data: '):
                    try:
                        kinds.append(json.loads(raw[6:].strip()).get('type'))
                    except ValueError:
                        pass

        self.assertNotIn('chart_unavailable', kinds)


class RenderingTests(TestCase):
    """The browser has to know what to do with the event."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'cr_patient', email='cr@test.invalid', password='pw', role='patient')

    def test_the_chat_page_handles_the_event(self):
        self.client.force_login(self.patient)
        body = self.client.get('/assistant/').content.decode()

        self.assertIn("payload.type === 'chart_unavailable'", body)
        self.assertIn('function renderChartUnavailable', body)

    def test_the_reason_is_rendered_as_text_not_html(self):
        """
        The reason names a biomarker traceable to an uploaded document, so it
        is attacker-influenced — the same reasoning that made renderChart use
        textContent.
        """
        self.client.force_login(self.patient)
        body = self.client.get('/assistant/').content.decode()

        start = body.index('function renderChartUnavailable')
        fn = body[start:start + 700]
        self.assertIn('textContent', fn)
        self.assertNotIn('innerHTML', fn)

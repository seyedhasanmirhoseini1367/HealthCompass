"""
A failed search must not be reported as an empty one.

Found in production. The Gemini embedding quota was exhausted, so
`RetrievalService.retrieve` raised; `_retrieve` in the graph caught it and
returned an empty chunk list — the same value a patient with no matching
records produces. Generation then ran with no context and told the patient:

    "I've checked your medical records, but it appears there are no recent
     lab results available."

The records existed. That sentence is a claim about the patient's health,
produced by an infrastructure failure, and nothing distinguished it from the
truth — not to the patient, and not to us afterwards.

This is the same family as CB-2 (embedding failure hid a record permanently)
arriving by a third route, and it breaks two standing rules at once: an
infrastructure failure became a clinical answer, and missing data was treated
as a negative finding.

The chunk list alone cannot carry this distinction, so `retrieval_failed` does,
and `stream_graph` refuses rather than generating.
"""
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.rag_assistant.graph.graph import RETRIEVAL_UNAVAILABLE_MESSAGE

User = get_user_model()

_NODES = 'apps.rag_assistant.graph.nodes'


class RetrieveNodeTests(TestCase):
    """The flag has to be set where the failure actually happens."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'ru_patient', email='ru_patient@test.invalid', password='pw',
            role='patient')
        self.state = {'question': 'What do my labs show?', 'patient_id': self.patient.pk,
                      'rewritten_query': '', 'temporal_mode': None}

    def test_a_successful_search_is_not_flagged(self):
        from apps.rag_assistant.graph.nodes import _retrieve

        with patch(f'{_NODES}.HealthState', create=True):
            with patch('apps.rag_assistant.services.retrieval_service.RetrievalService') as svc:
                svc.return_value.retrieve.return_value = [{'text': 'Glucose 5.2'}]
                result = _retrieve(self.state)

        self.assertFalse(result['retrieval_failed'])
        self.assertEqual(len(result['context_chunks']), 1)

    def test_an_empty_result_is_not_a_failure(self):
        """A patient with nothing relevant is a real, correct answer."""
        from apps.rag_assistant.graph.nodes import _retrieve

        with patch('apps.rag_assistant.services.retrieval_service.RetrievalService') as svc:
            svc.return_value.retrieve.return_value = []
            result = _retrieve(self.state)

        self.assertFalse(result['retrieval_failed'])
        self.assertEqual(result['context_chunks'], [])

    def test_a_raised_search_is_flagged(self):
        """ACCEPTANCE. This returned an empty list, indistinguishable from above."""
        from apps.rag_assistant.graph.nodes import _retrieve

        with patch('apps.rag_assistant.services.retrieval_service.RetrievalService') as svc:
            svc.return_value.retrieve.side_effect = RuntimeError(
                'Gemini embedding error: 429 RESOURCE_EXHAUSTED')
            result = _retrieve(self.state)

        self.assertTrue(result['retrieval_failed'])
        self.assertEqual(result['context_chunks'], [])

    def test_the_failure_is_recorded_as_an_operational_event(self):
        """An outage that only shows up as a bad answer is one nobody notices."""
        from apps.rag_assistant.graph.nodes import _retrieve

        with patch('apps.rag_assistant.services.retrieval_service.RetrievalService') as svc:
            svc.return_value.retrieve.side_effect = RuntimeError('quota exhausted')
            with self.assertLogs('healthcompass.ops', level='ERROR') as logs:
                _retrieve(self.state)

        self.assertTrue(any('RETRIEVAL_FAILED' in line for line in logs.output))

    def test_the_event_does_not_carry_the_question_text(self):
        """The query is patient-authored content and must not reach a log drain."""
        from apps.rag_assistant.graph.nodes import _retrieve

        self.state['question'] = 'Do my results mean I have cancer?'
        with patch('apps.rag_assistant.services.retrieval_service.RetrievalService') as svc:
            svc.return_value.retrieve.side_effect = RuntimeError('quota exhausted')
            with self.assertLogs('healthcompass.ops', level='ERROR') as logs:
                _retrieve(self.state)

        joined = '\n'.join(logs.output)
        self.assertNotIn('cancer', joined)


class StreamGraphRefusalTests(TestCase):
    """What the patient is actually told."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'ru_stream', email='ru_stream@test.invalid', password='pw', role='patient')

    def _events(self, rstate):
        from apps.rag_assistant.graph.graph import stream_graph

        events = []
        with patch('apps.rag_assistant.graph.graph.health_graph_routing') as routing:
            routing.invoke.return_value = rstate
            with patch('apps.rag_assistant.services.guardrail_service.GuardrailService') as grd:
                grd.check_pre_query.return_value = (False, '')
                instance = MagicMock()
                instance.soften_stream_prefix.side_effect = (
                    lambda pending, final=False: (pending, ''))
                instance.get_appended_disclaimers.return_value = ('', [])
                grd.return_value = instance
                for chunk in stream_graph(query='What do my labs show?',
                                          patient=self.patient):
                    for line in chunk.splitlines():
                        if line.startswith('data: '):
                            try:
                                events.append(json.loads(line[6:]))
                            except json.JSONDecodeError:
                                pass
        return events

    def _failed_state(self):
        return {'route': 'lab_results', 'context_chunks': [], 'retrieval_failed': True,
                'trajectory_context': '', 'mode': 'personal', 'rewritten_query': '',
                'answer': ''}

    def test_the_patient_is_told_the_search_failed(self):
        """ACCEPTANCE — the sentence that started this."""
        text = ''.join(e.get('content', '') for e in self._events(self._failed_state())
                       if e.get('type') == 'token')

        self.assertIn("couldn't search your medical records", text)
        self.assertNotIn('no recent lab results', text.lower())

    def test_it_does_not_claim_anything_about_the_records(self):
        text = ''.join(e.get('content', '') for e in self._events(self._failed_state())
                       if e.get('type') == 'token').lower()

        for claim in ('there are no', 'you have no', 'appears there are no',
                      'no lab results', 'nothing was found'):
            self.assertNotIn(claim, text, f'still asserts {claim!r} about the patient')

    def test_it_says_the_fault_is_ours(self):
        text = ''.join(e.get('content', '') for e in self._events(self._failed_state())
                       if e.get('type') == 'token').lower()
        self.assertIn('our side', text)

    def test_the_mode_is_reported_so_the_client_can_tell(self):
        meta = next(e for e in self._events(self._failed_state()) if e.get('type') == 'meta')
        self.assertEqual(meta['mode'], 'retrieval_unavailable')
        self.assertIn('retrieval_unavailable', meta['triggered_rules'])

    def test_no_sources_are_offered(self):
        sources = next(e for e in self._events(self._failed_state())
                       if e.get('type') == 'sources')
        self.assertEqual(sources['sources'], [])

    def test_the_stream_still_terminates_cleanly(self):
        """A client waiting on `done` must not hang because of an outage."""
        kinds = [e.get('type') for e in self._events(self._failed_state())]
        self.assertEqual(kinds[-1], 'done')

    def test_no_llm_is_called_when_retrieval_failed(self):
        """
        Generation with an empty context is what produced the false answer.
        It must not run at all — and not spend a provider call either.
        """
        with patch('apps.rag_assistant.services.generation_service.generate_streaming') as gen:
            self._events(self._failed_state())
            gen.assert_not_called()

    def test_a_genuinely_empty_result_still_answers_normally(self):
        """
        The whole point is the distinction. A patient with no matching records
        must still get a real answer, not the outage message.
        """
        ok_state = {'route': 'lab_results', 'context_chunks': [], 'retrieval_failed': False,
                    'trajectory_context': '', 'mode': 'personal', 'rewritten_query': '',
                    'answer': ''}

        with patch('apps.rag_assistant.services.generation_service.generate_streaming',
                   side_effect=lambda *a, **kw: iter(['You have no matching records.'])):
            with patch('apps.rag_assistant.services.generation_service.active_stream_provider',
                       return_value='groq'):
                with patch('apps.rag_assistant.services.generation_service._build_sources',
                           return_value=[]):
                    events = self._events(ok_state)

        text = ''.join(e.get('content', '') for e in events if e.get('type') == 'token')
        self.assertNotIn("couldn't search", text)

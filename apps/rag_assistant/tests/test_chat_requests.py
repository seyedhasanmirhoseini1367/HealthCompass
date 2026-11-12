"""Request-level chat tests, web and API (audit T-07, prompt 9 task 2)."""
import json
from unittest import mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.consent import grant_consent
from apps.accounts.models import ConsentPurpose
from apps.rag_assistant.models import ChatSession, QueryLog
from apps.rag_assistant.services.chat_service import HISTORY_TURNS, MAX_QUESTION_CHARS
from apps.testing.factories import make_patient


def _events(*tokens, done=True):
    for t in tokens:
        yield f'data: {json.dumps({"type": "token", "content": t})}\n\n'
    yield f'data: {json.dumps({"type": "meta", "provider": "stub", "chunks": 1, "mode": "personal"})}\n\n'
    if done:
        yield 'data: {"type": "done"}\n\n'


class _Chat(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_patient()
        grant_consent(self.user, ConsentPurpose.EXTERNAL_LLM)
        self.client.force_login(self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def web_stream(self, message, **extra):
        response = self.client.post('/assistant/stream/', json.dumps({'message': message, **extra}),
                                    content_type='application/json')
        if response.status_code == 200:
            b''.join(response.streaming_content)
        return response


class StreamPersistenceTests(_Chat):
    def test_a_stream_saves_exactly_one_log_with_the_collected_tokens(self):
        with patch('apps.rag_assistant.services.rag_service.RAGService.stream_ask',
                   return_value=_events('Hello ', 'there')):
            self.web_stream('hi')
        log = QueryLog.objects.get()
        self.assertEqual(log.response, 'Hello there')
        self.assertEqual(log.subject, self.user)

    def test_a_client_disconnect_still_saves_what_was_streamed(self):
        with patch('apps.rag_assistant.services.rag_service.RAGService.stream_ask',
                   return_value=_events('Partial', done=False)):
            response = self.client.post('/assistant/stream/', json.dumps({'message': 'hi'}),
                                        content_type='application/json')
            next(iter(response.streaming_content))
            # What the server does when the client goes away. close() fires
            # request_finished, whose close_old_connections() would close the
            # test's own transaction on PostgreSQL; keep the connection open.
            with mock.patch('django.db.backends.base.base.BaseDatabaseWrapper.close_if_unusable_or_obsolete'):
                response.close()
        self.assertEqual(QueryLog.objects.get().response, 'Partial')

    def test_an_empty_answer_is_not_saved_and_sends_an_error(self):
        with patch('apps.rag_assistant.services.rag_service.RAGService.stream_ask',
                   return_value=_events()):
            response = self.client.post('/assistant/stream/', json.dumps({'message': 'hi'}),
                                        content_type='application/json')
            body = b''.join(response.streaming_content).decode()
        self.assertIn('"type": "error"', body)
        self.assertFalse(QueryLog.objects.exists())

    def test_api_stream_persists_too(self):
        with patch('apps.rag_assistant.services.rag_service.RAGService.stream_ask',
                   return_value=_events('API answer')):
            response = self.api.post('/api/v1/assistant/stream/', {'query': 'hi'}, format='json')
            b''.join(response.streaming_content)
        self.assertEqual(QueryLog.objects.get().response, 'API answer')


class InputRulesTests(_Chat):
    def test_an_over_long_question_is_400_on_both(self):
        long = 'x' * (MAX_QUESTION_CHARS + 1)
        self.assertEqual(self.web_stream(long).status_code, 400)
        self.assertEqual(self.api.post('/api/v1/assistant/ask/', {'query': long}, format='json').status_code, 400)

    def test_a_subject_the_user_may_not_ask_about_is_404(self):
        stranger = make_patient()
        self.assertEqual(self.web_stream('hi', subject=str(stranger.pk)).status_code, 404)
        response = self.api.post('/api/v1/assistant/ask/', {'query': 'hi', 'subject': str(stranger.pk)},
                                 format='json')
        self.assertEqual(response.status_code, 404)

    def test_history_is_limited_and_subject_scoped(self):
        session = ChatSession.objects.create(patient=self.user, title='s')
        other = make_patient()
        for i in range(HISTORY_TURNS + 3):
            QueryLog.objects.create(session=session, query=f'q{i}', response='a', subject=self.user)
        QueryLog.objects.create(session=session, query='about-other', response='a', subject=other)
        seen = {}

        def fake_ask(self_, person, query, history):
            seen['history'] = history
            return 'ok', [], 'stub', 1, False, []
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask', fake_ask):
            self.api.post('/api/v1/assistant/ask/', {'query': 'next', 'session_id': str(session.pk)},
                          format='json')
        self.assertEqual(len(seen['history']), HISTORY_TURNS)
        self.assertNotIn('about-other', [h['query'] for h in seen['history']])

"""
Chat session handling shared by the web chat and the mobile API (audit CQ-02).

The two surfaces each resolved sessions, loaded history, collected streamed
tokens and saved QueryLog rows on their own, and drifted: 3 history turns on
the web and 5 on the API, titles of 50, 60 and 200 characters, a question-length
limit only on the web, and an empty answer saved as if it were one. All of that
now happens here, once.
"""
import json
import logging

from django.core.exceptions import ValidationError

from ..models import ChatSession, QueryLog

logger = logging.getLogger(__name__)

HISTORY_TURNS = 3
TITLE_MAX = 60
RENAME_TITLE_MAX = 100
MAX_QUESTION_CHARS = 4000
EMPTY_ANSWER_MESSAGE = 'No answer was generated. Please try again.'


def question_error(query: str):
    """A user-facing reason this question cannot be asked, or None."""
    if not query:
        return 'Please enter a question.'
    if len(query) > MAX_QUESTION_CHARS:
        return (f'Message is too long ({len(query)} characters). '
                f'Please keep questions under {MAX_QUESTION_CHARS}.')
    return None


def resolve_session(user, session_id, query: str) -> ChatSession:
    """The caller's own session with this id, or a new one titled from the question."""
    if session_id:
        try:
            return ChatSession.objects.get(pk=session_id, patient=user)
        except (ChatSession.DoesNotExist, ValidationError, ValueError):
            pass
    return ChatSession.objects.create(patient=user, title=query[:TITLE_MAX])


def load_history(session, subject=None) -> list[dict]:
    """The last HISTORY_TURNS exchanges, oldest first. When the log records whose
    data each answer used, only turns about the same subject are replayed, so
    two people's records never meet in one prompt."""
    qs = session.messages.all()
    if subject is not None:
        qs = qs.filter(subject=subject)
    history = list(qs.values('query', 'response').order_by('-created_at')[:HISTORY_TURNS])
    history.reverse()
    return history


def persist(session, query, response, *, subject=None, sources=None, provider='',
            chunks=0, safety_routed=False, triggered_rules=None, mode='personal',
            chart_data=None):
    """Save one exchange. An empty answer is not saved: it would replay as
    history and show in the transcript as if the assistant had said nothing."""
    if not (response or '').strip():
        return None
    if not session.messages.exists():
        session.title = query[:TITLE_MAX]
        session.save(update_fields=['title'])
    fields = dict(session=session, query=query, response=response, sources=sources or [],
                  llm_provider=provider, retrieved_chunks_count=chunks,
                  safety_routed=safety_routed, triggered_rules=triggered_rules or [],
                  query_mode=mode, chart_data=chart_data)
    if subject is not None:
        fields['subject'] = subject
    return QueryLog.objects.create(**fields)


def ask(user, session, query, subjects):
    """Answer for each subject, labelled when there is more than one. Returns
    (response_text, sources, provider, log)."""
    from apps.rag_assistant import subject as subject_mod

    from .rag_service import RAGService

    answers, sources, provider, logs = [], [], '', []
    labelled = len(subjects) > 1
    for person in subjects:
        subject_mod.record_access(user, person, query)
        history = load_history(session, person)
        text, srcs, prov, n, routed, triggered = RAGService().ask(person, query, history)
        name = subject_mod.label_for(user, person)
        answers.append((name, text))
        sources.extend(srcs)
        provider = provider or prov
        # One log row per person, so each answer can be found, revoked and
        # erased with the person it is about (audit SEC-12).
        log = persist(session, query, f'**{name}**\n\n{text}' if labelled else text,
                      subject=person, sources=srcs, provider=prov, chunks=n or 0,
                      safety_routed=routed, triggered_rules=triggered)
        if log is not None:
            logs.append(log)

    if not labelled:
        response_text = answers[0][1]
    else:
        # Labelled, never merged: attributing a drug to the wrong person is a
        # clinical error, not a formatting one.
        response_text = '\n\n'.join(f'**{name}**\n\n{text}' for name, text in answers)
    return response_text, sources, provider, (logs[0] if logs else None)


def stream(user, session, query, stream_subject):
    """SSE generator for one subject. Yields the RAG events unchanged and saves
    the exchange when the stream ends, including after a client disconnect.
    An empty answer is not saved; an error event is sent instead."""
    from apps.rag_assistant import subject as subject_mod

    from .rag_service import RAGService

    subject_mod.record_access(user, stream_subject, query)
    history = load_history(session, stream_subject)
    from django.conf import settings
    stub_delay = float(getattr(settings, 'LOADTEST_STUB_LLM_DELAY', 0) or 0)
    if stub_delay and settings.DEBUG:
        # Load testing only (loadtest/README.md): a fixed answer after a delay,
        # so a run measures this server, not a provider. Requires DEBUG.
        import time
        time.sleep(stub_delay)
        yield _sse({'type': 'token', 'content': 'Stub answer.'})
        yield _sse({'type': 'done'})
        persist(session, query, 'Stub answer.', subject=stream_subject)
        return
    collected = {'tokens': [], 'sources': [], 'provider': '', 'chunks': 0,
                 'safety_routed': False, 'rules': [], 'mode': 'personal', 'chart': None}
    errored = False
    try:
        for event_str in RAGService().stream_ask(patient=stream_subject, query=query, history=history):
            payload = _parse_event(event_str)
            kind = payload.get('type') if payload else None
            if kind == 'done' and not ''.join(collected['tokens']).strip() and not errored:
                errored = True
                yield _sse({'type': 'error', 'message': EMPTY_ANSWER_MESSAGE, 'retry': True})
            # Record before yielding: if the client disconnects at this yield,
            # what it already received is still saved.
            if kind == 'token':
                collected['tokens'].append(payload.get('content', ''))
            elif kind == 'sources':
                collected['sources'].extend(payload.get('sources', []))
            elif kind == 'chart':
                collected['chart'] = payload.get('chart')
            elif kind == 'error':
                errored = True
            elif kind == 'meta':
                collected.update(provider=payload.get('provider', ''),
                                 chunks=payload.get('chunks', 0),
                                 safety_routed=payload.get('safety_routed', False),
                                 rules=payload.get('triggered_rules', []),
                                 mode=payload.get('mode', 'personal'))
            yield event_str
    finally:
        try:
            persist(session, query, ''.join(collected['tokens']), subject=stream_subject,
                    sources=collected['sources'], provider=collected['provider'],
                    chunks=collected['chunks'], safety_routed=collected['safety_routed'],
                    triggered_rules=collected['rules'], mode=collected['mode'],
                    chart_data=collected['chart'])
        except Exception:
            logger.exception('Failed to save streamed QueryLog')


def _parse_event(event_str):
    raw = (event_str or '').strip()
    if not raw.startswith('data: '):
        return None
    try:
        return json.loads(raw[6:])
    except (ValueError, TypeError):
        return None


def _sse(payload) -> str:
    return f'data: {json.dumps(payload)}\n\n'

import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .models import ChatSession, QueryLog
from .services.rag_service import RAGService
from apps.accounts.verification import require_verified_email
from healthcompass.net import scoped_ratelimit

_RATE_EXCEEDED = JsonResponse(
    {'error': 'Rate limit exceeded. Please wait a moment before sending another message.'},
    status=429,
)

logger = logging.getLogger(__name__)

#: Longest accepted question; defined once in chat_service for web and API.
from .services.chat_service import MAX_QUESTION_CHARS  # noqa: E402



# ─── Chat UI ──────────────────────────────────────────────────────────────────

@login_required
def chat_view(request):
    """Main chat interface. Creates or resumes a session."""
    session_id = request.GET.get('session')
    if session_id:
        session = get_object_or_404(ChatSession, pk=session_id, patient=request.user)
    else:
        # Get or create today's session
        session = ChatSession.objects.filter(patient=request.user).first()
        if not session:
            session = ChatSession.objects.create(patient=request.user, title='My Health Chat')

    chat_messages = list(session.messages.all())
    sessions = ChatSession.objects.filter(patient=request.user)[:10]

    has_records = request.user.medical_records.exists()

    # Per-message data for JS (mode badge, sources, chart) — avoids attribute escaping issues
    history_data = [
        {
            'query_mode': m.query_mode or 'personal',
            'sources':    m.sources or [],
            'chart_data': m.chart_data,
        }
        for m in chat_messages
    ]

    from apps.rag_assistant.subject import choices_for
    from apps.rag_assistant.suggestions import starter_questions

    try:
        suggestions = starter_questions(request.user) if not chat_messages else []
    except Exception:
        logger.exception('chat_view: starter questions failed')
        suggestions = []

    return render(request, 'rag_assistant/chat.html', {
        # Built from the same predicate the request path enforces, so nothing
        # can be offered that the check would then refuse.
        'subject_choices':  choices_for(request.user),
        'session':          session,
        'chat_messages':    chat_messages,
        'sessions':         sessions,
        'has_records':      has_records,
        'history_data': history_data,
        'suggestions':  suggestions,
    })


# ─── New session ──────────────────────────────────────────────────────────────

@login_required
@require_POST
def new_session(request):
    """
    Start a new chat session. POST only.

    This was a GET that created a database row, so
    <img src="/assistant/new/"> on any page created a ChatSession for every
    logged-in visitor who loaded it — unbounded, unrated, and CSRF-exempt by
    construction because GET is exempt.
    """
    session = ChatSession.objects.create(
        patient=request.user,
        title='New Chat',
    )
    return redirect(f'/assistant/?session={session.pk}')


# ─── Send message (AJAX) ──────────────────────────────────────────────────────

@login_required
@require_POST
@ratelimit(key='user', rate='20/m', block=False)
# Daily cap shared with the API's ai_daily scope (SEC-16).
@scoped_ratelimit('ai_daily')
@require_verified_email
def send_message(request):
    if getattr(request, 'limited', False):
        return _RATE_EXCEEDED
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    from apps.rag_assistant import subject as subject_mod
    from .services import chat_service

    query = (body.get('message') or '').strip()
    error = chat_service.question_error(query)
    if error:
        return JsonResponse({'error': error}, status=400)

    # Whose records: untrusted client input, checked against accounts.authz;
    # refused indistinguishably from "no such person".
    try:
        subjects = subject_mod.resolve(request.user, body.get('subject'))
    except subject_mod.SubjectNotPermitted:
        return JsonResponse({'error': 'Not available.'}, status=404)

    session = chat_service.resolve_session(request.user, body.get('session_id'), query)
    response_text, sources, _provider, log = chat_service.ask(request.user, session, query, subjects)
    if log is None:
        return JsonResponse({'error': chat_service.EMPTY_ANSWER_MESSAGE, 'retry': True,
                             'session_id': str(session.pk)}, status=502)
    return JsonResponse({
        'response': response_text,
        'sources': sources,
        'session_id': str(session.pk),
        'message_id': str(log.pk),
    })


# ─── Session history ──────────────────────────────────────────────────────────

@login_required
def session_history(request, pk):
    session = get_object_or_404(ChatSession, pk=pk, patient=request.user)
    messages = list(session.messages.values('query', 'response', 'created_at'))
    return JsonResponse({'messages': messages})


# ─── Rename session ───────────────────────────────────────────────────────────

@login_required
@require_POST
def rename_session(request, pk):
    session = get_object_or_404(ChatSession, pk=pk, patient=request.user)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    title = body.get('title', '').strip()
    if not title:
        return JsonResponse({'error': 'Title cannot be empty'}, status=400)
    from .services.chat_service import RENAME_TITLE_MAX
    session.title = title[:RENAME_TITLE_MAX]
    session.save(update_fields=['title'])
    return JsonResponse({'ok': True, 'title': session.title})


# ─── Delete session ───────────────────────────────────────────────────────────

@login_required
@require_POST
def delete_session(request, pk):
    session = get_object_or_404(ChatSession, pk=pk, patient=request.user)
    session.delete()
    return JsonResponse({'ok': True})


# ─── Stream message (SSE) ─────────────────────────────────────────────────────

@login_required
@require_POST
@ratelimit(key='user', rate='20/m', block=False)
# Daily cap shared with the API's ai_daily scope (SEC-16).
@scoped_ratelimit('ai_daily')
@require_verified_email
def stream_message(request):
    """
    SSE endpoint. Streams tokens from RAGService.stream_ask().

    Client receives a series of SSE events:
        data: {"type": "token",   "content": "..."}
        data: {"type": "sources", "sources": [...]}
        data: {"type": "done"}
        data: {"type": "error",   "message": "..."}

    After the stream finishes the full response is assembled from token
    events and saved to the DB as a QueryLog so chat history is consistent.
    """
    # The rate-limit check previously sat ABOVE this docstring, which made the
    # string a no-op expression rather than __doc__ — the SSE event shapes
    # documented here were invisible to help() and every IDE.
    if getattr(request, 'limited', False):
        return _RATE_EXCEEDED

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    from apps.rag_assistant import subject as subject_mod
    from .services import chat_service

    query = (body.get('message') or '').strip()
    error = chat_service.question_error(query)
    if error:
        return JsonResponse({'error': error}, status=400)

    try:
        subjects = subject_mod.resolve(request.user, body.get('subject'))
    except subject_mod.SubjectNotPermitted:
        return JsonResponse({'error': 'Not available.'}, status=404)

    # One subject per stream. "Everyone" uses the non-streaming path; here it
    # degrades to the asker rather than streaming somebody else's records.
    stream_subject = subjects[0] if len(subjects) == 1 else request.user
    session = chat_service.resolve_session(request.user, body.get('session_id'), query)

    def _event_stream():
        yield from chat_service.stream(request.user, session, query, stream_subject)

    response = StreamingHttpResponse(_event_stream(), content_type='text/event-stream')
    response['Cache-Control']    = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    response['X-Session-Id']     = str(session.pk)
    return response

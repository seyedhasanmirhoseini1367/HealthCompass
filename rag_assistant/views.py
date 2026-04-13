import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

from .models import ChatSession, QueryLog
from .services.rag_service import RAGService

logger = logging.getLogger(__name__)


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

    messages = session.messages.all()
    sessions = ChatSession.objects.filter(patient=request.user)[:10]

    has_records = request.user.medical_records.exists()

    return render(request, 'rag_assistant/chat.html', {
        'session': session,
        'messages': messages,
        'sessions': sessions,
        'has_records': has_records,
    })


# ─── New session ──────────────────────────────────────────────────────────────

@login_required
def new_session(request):
    session = ChatSession.objects.create(
        patient=request.user,
        title='New Chat',
    )
    return redirect(f'/assistant/?session={session.pk}')


# ─── Send message (AJAX) ──────────────────────────────────────────────────────

@login_required
@require_POST
def send_message(request):
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    query = body.get('message', '').strip()
    session_id = body.get('session_id')

    if not query:
        return JsonResponse({'error': 'Empty message'}, status=400)

    # Get or create session
    if session_id:
        try:
            session = ChatSession.objects.get(pk=session_id, patient=request.user)
        except ChatSession.DoesNotExist:
            session = ChatSession.objects.create(patient=request.user, title=query[:50])
    else:
        session = ChatSession.objects.create(patient=request.user, title=query[:50])

    # Get history for context
    history = list(session.messages.values('query', 'response').order_by('-created_at')[:3])
    history.reverse()

    # Call RAG service
    response_text, sources = RAGService().ask(request.user, query, history)

    # Auto-title the session on first message
    if session.messages.count() == 0:
        session.title = query[:60]
        session.save(update_fields=['title'])

    # Save to DB
    log = QueryLog.objects.create(
        session=session,
        query=query,
        response=response_text,
        sources=sources,
    )

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
    session.title = title[:100]
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
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    query      = body.get('message', '').strip()
    session_id = body.get('session_id')

    if not query:
        return JsonResponse({'error': 'Empty message'}, status=400)

    # Resolve / create session
    if session_id:
        try:
            session = ChatSession.objects.get(pk=session_id, patient=request.user)
        except ChatSession.DoesNotExist:
            session = ChatSession.objects.create(patient=request.user, title=query[:50])
    else:
        session = ChatSession.objects.create(patient=request.user, title=query[:50])

    history = list(session.messages.values('query', 'response').order_by('-created_at')[:3])
    history.reverse()

    def _event_stream():
        collected_tokens = []
        collected_sources = []

        try:
            for event_str in RAGService().stream_ask(
                patient  = request.user,
                query    = query,
                history  = history,
            ):
                # event_str is already SSE-formatted ("data: {...}\n\n")
                yield event_str

                # Parse to collect tokens + sources for DB save
                try:
                    raw = event_str.strip()
                    if raw.startswith('data: '):
                        payload = json.loads(raw[6:])
                        if payload.get('type') == 'token':
                            collected_tokens.append(payload.get('content', ''))
                        elif payload.get('type') == 'sources':
                            collected_sources.extend(payload.get('sources', []))
                except Exception:
                    pass  # parsing failure doesn't break the stream

        finally:
            # Persist response to DB once the stream is exhausted
            try:
                full_response = ''.join(collected_tokens)
                if session.messages.count() == 0:
                    session.title = query[:60]
                    session.save(update_fields=['title'])

                QueryLog.objects.create(
                    session  = session,
                    query    = query,
                    response = full_response,
                    sources  = collected_sources,
                )
            except Exception as exc:
                logger.exception('Failed to save streamed QueryLog: %s', exc)

    response = StreamingHttpResponse(_event_stream(), content_type='text/event-stream')
    response['Cache-Control']    = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    response['X-Session-Id']     = str(session.pk)
    return response

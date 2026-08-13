import json
import logging

from django.db.models import Count
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..throttling import AI_THROTTLES

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes(AI_THROTTLES)
def assistant_ask(request):
    from apps.rag_assistant.models import ChatSession, QueryLog
    from apps.rag_assistant.services.rag_service import RAGService

    query      = request.data.get('query', '').strip()
    session_id = request.data.get('session_id')
    if not query:
        return Response({'error': 'query is required.'}, status=status.HTTP_400_BAD_REQUEST)

    if session_id:
        try:
            session = ChatSession.objects.get(pk=session_id, patient=request.user)
        except Exception:
            session = ChatSession.objects.create(patient=request.user, title=query[:60])
    else:
        session = ChatSession.objects.create(patient=request.user, title=query[:60])

    history = list(
        session.messages.values('query', 'response').order_by('-created_at')[:5]
    )
    history.reverse()

    try:
        result          = RAGService().ask(request.user, query, history)
        response_text   = result[0]
        sources         = result[1]
        provider        = result[2] if len(result) > 2 else ''
        chunks_count    = result[3] if len(result) > 3 else None
        safety_routed   = result[4] if len(result) > 4 else False
        triggered_rules = result[5] if len(result) > 5 else []
    except Exception as exc:
        logger.exception('assistant_ask error')
        return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if not session.messages.exists():
        session.title = query[:60]
    session.save(update_fields=['title', 'updated_at'])

    log = QueryLog.objects.create(
        session                = session,
        query                  = query,
        response               = response_text,
        sources                = sources,
        llm_provider           = provider,
        retrieved_chunks_count = chunks_count,
        safety_routed          = safety_routed,
        triggered_rules        = triggered_rules,
    )

    payload = {
        'answer':     response_text,
        'sources':    sources,
        'session_id': str(session.pk),
        'message_id': str(log.pk),
    }
    if provider == 'consent_required':
        # Deliberately still 200 with the message in `answer`: existing chat
        # clients render it inline, which is better UX than an error toast.
        # The extra field lets newer clients show a dedicated consent prompt.
        payload['consent_required'] = 'external_llm'
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes(AI_THROTTLES)
def assistant_stream(request):
    """
    JWT-authenticated SSE counterpart to `assistant_ask`, for the mobile app.

    Wraps the same RAGService.stream_ask() generator the web chat UI uses
    (apps/rag_assistant/views.py::stream_message), so the event shape is
    identical:

        data: {"type": "token",   "content": "..."}
        data: {"type": "sources", "sources": [...]}
        data: {"type": "meta",    "provider": "...", "chunks": N, "mode": "..."}
        data: {"type": "chart",   "chart": {...}}
        data: {"type": "done"}
        data: {"type": "error",   "message": "..."}
    """
    from apps.rag_assistant.models import ChatSession, QueryLog
    from apps.rag_assistant.services.rag_service import RAGService

    query      = request.data.get('query', '').strip()
    session_id = request.data.get('session_id')
    if not query:
        return Response({'error': 'query is required.'}, status=status.HTTP_400_BAD_REQUEST)

    if session_id:
        try:
            session = ChatSession.objects.get(pk=session_id, patient=request.user)
        except Exception:
            session = ChatSession.objects.create(patient=request.user, title=query[:60])
    else:
        session = ChatSession.objects.create(patient=request.user, title=query[:60])

    history = list(
        session.messages.values('query', 'response').order_by('-created_at')[:5]
    )
    history.reverse()

    def _event_stream():
        collected_tokens          = []
        collected_sources         = []
        collected_provider        = ''
        collected_chunks          = 0
        collected_safety_routed   = False
        collected_triggered_rules = []
        collected_mode            = 'personal'
        collected_chart_data      = None

        try:
            for event_str in RAGService().stream_ask(
                patient  = request.user,
                query    = query,
                history  = history,
            ):
                yield event_str
                try:
                    raw = event_str.strip()
                    if raw.startswith('data: '):
                        payload = json.loads(raw[6:])
                        t = payload.get('type')
                        if t == 'token':
                            collected_tokens.append(payload.get('content', ''))
                        elif t == 'sources':
                            collected_sources.extend(payload.get('sources', []))
                        elif t == 'chart':
                            collected_chart_data = payload.get('chart')
                        elif t == 'meta':
                            collected_provider        = payload.get('provider', '')
                            collected_chunks          = payload.get('chunks', 0)
                            collected_safety_routed   = payload.get('safety_routed', False)
                            collected_triggered_rules = payload.get('triggered_rules', [])
                            collected_mode            = payload.get('mode', 'personal')
                except Exception:
                    pass  # parsing failure doesn't break the stream
        finally:
            try:
                full_response = ''.join(collected_tokens)
                if session.messages.count() == 0:
                    session.title = query[:60]
                    session.save(update_fields=['title'])

                QueryLog.objects.create(
                    session                = session,
                    query                  = query,
                    response               = full_response,
                    sources                = collected_sources,
                    llm_provider           = collected_provider,
                    retrieved_chunks_count = collected_chunks,
                    safety_routed          = collected_safety_routed,
                    triggered_rules        = collected_triggered_rules,
                    query_mode             = collected_mode,
                    chart_data             = collected_chart_data,
                )
            except Exception:
                logger.exception('Failed to save streamed QueryLog')

    response = StreamingHttpResponse(_event_stream(), content_type='text/event-stream')
    response['Cache-Control']     = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    response['X-Session-Id']      = str(session.pk)
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def assistant_sessions(request):
    from apps.rag_assistant.models import ChatSession
    sessions = (ChatSession.objects
                .filter(patient=request.user)
                .annotate(message_count=Count('messages'))[:20])
    return Response({'sessions': [
        {
            'id':            str(s.pk),
            'title':         s.title,
            'created_at':    s.created_at.isoformat(),
            'updated_at':    s.updated_at.isoformat(),
            'message_count': s.message_count,
        }
        for s in sessions
    ]})


@api_view(['GET', 'DELETE', 'PATCH'])
@permission_classes([IsAuthenticated])
def assistant_session_detail(request, session_id):
    from django.core.exceptions import ValidationError
    from apps.rag_assistant.models import ChatSession
    try:
        session = ChatSession.objects.get(pk=session_id, patient=request.user)
    except (ChatSession.DoesNotExist, ValidationError, ValueError):
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'DELETE':
        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    if request.method == 'PATCH':
        title = request.data.get('title', '').strip()
        if not title:
            return Response({'error': 'Title is required.'}, status=status.HTTP_400_BAD_REQUEST)
        session.title = title[:200]
        session.save(update_fields=['title'])
        return Response({'id': str(session.pk), 'title': session.title})

    messages = list(session.messages.values('id', 'query', 'response', 'created_at').order_by('created_at'))
    return Response({
        'id':       str(session.pk),
        'title':    session.title,
        'messages': [
            {
                'id':         str(m['id']),
                'query':      m['query'],
                'response':   m['response'],
                'created_at': m['created_at'].isoformat() if m['created_at'] else None,
            }
            for m in messages
        ],
    })

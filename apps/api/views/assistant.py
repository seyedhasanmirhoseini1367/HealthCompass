import logging

from django.db.models import Count
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..throttling import AI_THROTTLES
from healthcompass.errors import client_error
from apps.accounts.verification import require_verified_email

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes(AI_THROTTLES)
@require_verified_email
def assistant_ask(request):
    from apps.rag_assistant import subject as subject_mod
    from apps.rag_assistant.services import chat_service

    query = str(request.data.get('query', '')).strip()
    error = chat_service.question_error(query)
    if error:
        return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)
    try:
        subjects = subject_mod.resolve(request.user, request.data.get('subject'))
    except subject_mod.SubjectNotPermitted:
        return Response({'error': 'Not available.'}, status=status.HTTP_404_NOT_FOUND)

    session = chat_service.resolve_session(request.user, request.data.get('session_id'), query)
    try:
        response_text, sources, provider, log = chat_service.ask(
            request.user, session, query, subjects)
    except Exception as exc:
        logger.exception('assistant_ask error')
        return Response(client_error(exc, context='assistant'),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    if log is None:
        return Response({'error': chat_service.EMPTY_ANSWER_MESSAGE, 'retry': True,
                         'session_id': str(session.pk)}, status=status.HTTP_502_BAD_GATEWAY)

    payload = {
        'answer':     response_text,
        'sources':    sources,
        'session_id': str(session.pk),
        'message_id': str(log.pk),
    }
    if provider == 'consent_required':
        # Still 200 with the message in `answer`: existing clients render it
        # inline; newer clients can branch on this field.
        payload['consent_required'] = 'external_llm'
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes(AI_THROTTLES)
@require_verified_email
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
    from apps.rag_assistant import subject as subject_mod
    from apps.rag_assistant.services import chat_service

    query = str(request.data.get('query', '')).strip()
    error = chat_service.question_error(query)
    if error:
        return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)
    try:
        subjects = subject_mod.resolve(request.user, request.data.get('subject'))
    except subject_mod.SubjectNotPermitted:
        return Response({'error': 'Not available.'}, status=status.HTTP_404_NOT_FOUND)
    stream_subject = subjects[0] if len(subjects) == 1 else request.user
    session = chat_service.resolve_session(request.user, request.data.get('session_id'), query)

    def _event_stream():
        yield from chat_service.stream(request.user, session, query, stream_subject)

    response = StreamingHttpResponse(_event_stream(), content_type='text/event-stream')
    response['Cache-Control']     = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    response['X-Session-Id']      = str(session.pk)
    return response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def assistant_sessions(request):
    from apps.rag_assistant.models import ChatSession
    from ..pagination import StandardPagination
    paginator = StandardPagination()
    qs = (ChatSession.objects.filter(patient=request.user)
          .annotate(message_count=Count('messages')).order_by('-updated_at'))
    try:
        sessions = paginator.paginate_queryset(qs, request)
        meta = {'count': paginator.page.paginator.count,
                'next': paginator.get_next_link(), 'previous': paginator.get_previous_link()}
    except Exception:  # out-of-range page
        sessions, meta = [], {'count': qs.count(), 'next': None, 'previous': None}
    return Response({**meta, 'sessions': [
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
        from apps.rag_assistant.services.chat_service import RENAME_TITLE_MAX
        session.title = title[:RENAME_TITLE_MAX]
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

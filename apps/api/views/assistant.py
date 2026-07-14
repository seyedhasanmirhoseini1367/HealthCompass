import logging

from django.db.models import Count
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

logger = logging.getLogger(__name__)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
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
        except ChatSession.DoesNotExist:
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

    return Response({
        'answer':     response_text,
        'sources':    sources,
        'session_id': str(session.pk),
        'message_id': str(log.pk),
    })


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
    from apps.rag_assistant.models import ChatSession
    try:
        session = ChatSession.objects.get(pk=session_id, patient=request.user)
    except ChatSession.DoesNotExist:
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

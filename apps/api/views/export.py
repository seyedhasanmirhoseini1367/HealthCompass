"""
Data export endpoints for the mobile client.

Generation is synchronous — the archive is built and streamed in one request, so
there is no job id, no stored artifact and no expiring URL to secure. `status`
therefore reports `ready` immediately; it exists so a client can show the
categories and the estimated shape of the archive before downloading, and so the
contract has somewhere to grow if generation ever moves to a queue.

The subject is always request.user. No endpoint accepts a user identifier.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from apps.accounts.export import CATEGORIES, EXCLUSIONS, EXPORT_VERSION, build_export

logger = logging.getLogger(__name__)


class ExportThrottle(UserRateThrottle):
    """Building an archive reads every record and file the user owns."""
    scope = 'export'


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@throttle_classes([ExportThrottle])
def export_status(request):
    """What an export would contain. Always `ready` — generation is synchronous."""
    return Response({
        'state':           'ready',
        'export_version':  EXPORT_VERSION,
        'download_url':    '/api/v1/export/download/',
        'format':          'application/zip',
        'data_categories': [name for name, _filename, _builder in CATEGORIES],
        'exclusions':      EXCLUSIONS,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@throttle_classes([ExportThrottle])
def export_download(request):
    """Stream the caller's own data archive."""
    from django.http import FileResponse

    try:
        archive, filename = build_export(request.user)
    except Exception:
        # Logged without archive contents — only the failure and the user id.
        logger.exception('Data export failed for user %s', request.user.pk)
        return Response(
            {'error': 'Export generation failed. Please try again later.',
             'state': 'failed'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    response = FileResponse(archive, as_attachment=True, filename=filename,
                            content_type='application/zip')
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    return response

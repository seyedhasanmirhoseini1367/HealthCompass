"""Error handlers (audit polish): standalone pages, a reference id on 500,
Retry-After on 429, and a readable CSRF failure page."""
import logging
import sys
import uuid

from django.http import HttpResponse
from django.template.loader import render_to_string


def render(request, template, context=None, status=200):
    """No request context on purpose: context processors read the user, the
    session and the database, any of which may be what failed."""
    return HttpResponse(render_to_string(template, context or {}), status=status)

logger = logging.getLogger(__name__)


def bad_request(request, exception=None):
    return render(request, '400.html', status=400)


def permission_denied(request, exception=None):
    return render(request, '403.html', status=403)


def page_not_found(request, exception=None):
    return render(request, '404.html', status=404)


def server_error(request):
    reference = uuid.uuid4().hex[:12]
    exc = sys.exc_info()[1]
    logger.error('Unhandled error [ref=%s] on %s', reference, request.path,
                 exc_info=exc if exc else None)
    return render(request, '500.html', {'reference': reference}, status=500)


def csrf_failure(request, reason=''):
    return render(request, 'csrf_failure.html', status=403)


def ratelimited(request, exception=None):
    """django-ratelimit's RATELIMIT_VIEW: 429 with Retry-After, not a 403."""
    response = render(request, '429.html', {'retry_after': 60}, status=429)
    response['Retry-After'] = '60'
    return response

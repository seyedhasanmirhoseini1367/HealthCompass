"""Receiver for Content-Security-Policy violation reports (report-only mode)."""
import json
import logging

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .net import ratelimit_ip_key

logger = logging.getLogger('healthcompass.csp')

MAX_REPORT_BYTES = 16 * 1024


# csrf_exempt: browsers send these reports themselves, without a token, and the
# endpoint only writes a log line.
@csrf_exempt
@require_POST
@ratelimit(key=ratelimit_ip_key, rate='30/m', method='POST', block=False)
def csp_report(request):
    if getattr(request, 'limited', False) or len(request.body) > MAX_REPORT_BYTES:
        return HttpResponse(status=204)
    try:
        report = json.loads(request.body or b'{}').get('csp-report', {})
    except (ValueError, AttributeError):
        return HttpResponse(status=204)
    if isinstance(report, dict):
        logger.warning('CSP violation: directive=%s blocked=%s page=%s',
                       str(report.get('violated-directive', ''))[:100],
                       str(report.get('blocked-uri', ''))[:200],
                       str(report.get('document-uri', '')).split('?')[0][:200])
    return HttpResponse(status=204)

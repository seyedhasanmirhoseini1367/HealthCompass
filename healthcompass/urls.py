import logging

from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static
from django.http import FileResponse, Http404, JsonResponse
from django.core.exceptions import PermissionDenied
from pathlib import Path
from apps.dashboard.landing import home
from apps.accounts.views import AutoCompleteSocialSignup


def health(request):
    """
    Liveness: is this process running?

    Deliberately checks nothing external. Railway restarts the service when this
    fails, and restarting does not fix a down database — it just removes the
    instance that could have served cached pages and clear error messages.
    Dependency state belongs in /health/ready/.
    """
    return JsonResponse({'status': 'ok'})


def readiness(request):
    """
    Readiness: can this process actually serve requests?

    Checks the dependencies whose absence makes the app non-functional rather
    than merely degraded. Returns 503 with a per-check breakdown so an operator
    (or an uptime monitor) can see which dependency is at fault without reading
    logs. Never returns detail about the failure itself, only which check failed.
    """
    from django.core.cache import cache
    from django.db import connection

    checks = {}

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        checks['database'] = 'ok'
    except Exception:
        # The exception text can carry connection strings; log it, do not return it.
        logging.getLogger(__name__).exception('readiness: database check failed')
        checks['database'] = 'error'

    try:
        cache.set('healthcheck:ready', '1', 10)
        checks['cache'] = 'ok' if cache.get('healthcheck:ready') == '1' else 'error'
    except Exception:
        logging.getLogger(__name__).exception('readiness: cache check failed')
        checks['cache'] = 'error'

    ready = all(v == 'ok' for v in checks.values())
    return JsonResponse(
        {'status': 'ready' if ready else 'not_ready', 'checks': checks},
        status=200 if ready else 503,
    )


def _user_can_access_media(user, relative_path: str) -> bool:
    """
    Return True if user is allowed to download this media file.

    The rule lives in apps.accounts.authz so the web and API surfaces cannot
    drift apart. Two things changed there: `is_staff` no longer grants a silent
    bypass (it still grants access, but the access is recorded), and a doctor
    with an ACTIVE patient link can now open the file behind a record they are
    already permitted to read.
    """
    from apps.accounts.authz import can_access_media
    return can_access_media(user, relative_path)


def serve_media(request, path):
    # Must be authenticated
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())

    # Path traversal protection: resolve and confirm it stays inside MEDIA_ROOT.
    # Uses Path.is_relative_to rather than string prefixing — the old check
    # hardcoded '/' as the separator, so on Windows it rejected every legitimate
    # path while a POSIX-style prefix match is also vulnerable to sibling
    # directories such as /media-evil/ matching /media.
    media_root = Path(settings.MEDIA_ROOT).resolve()
    try:
        requested = (media_root / path).resolve()
    except Exception:
        raise Http404
    if requested != media_root and not requested.is_relative_to(media_root):
        raise Http404

    if not requested.is_file():
        raise Http404

    # Ownership check
    if not _user_can_access_media(request.user, path):
        raise PermissionDenied

    return _safe_file_response(requested)


# Content types we are willing to let a browser render inline. Everything else —
# and anything unrecognised — is sent as an opaque download.
#
# SVG is deliberately absent: it is an XML document that can carry <script>, so
# serving one from our own origin would run attacker script as first-party code.
# HTML is absent for the same reason.
_INLINE_SAFE_TYPES = {
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif':  'image/gif',
    '.webp': 'image/webp',
    # PDF stays inline so people can still read their own lab reports in the
    # browser. A PDF can embed JavaScript, but browser PDF viewers run it in
    # their own sandbox with no access to this origin's DOM or cookies, and the
    # CSP sandbox header below applies on top of that.
    '.pdf':  'application/pdf',
}


def _safe_file_response(path: Path):
    """
    Serve an uploaded file without letting it become active web content.

    Uploads are user-controlled bytes living on our origin. Two rules keep them
    inert: never advertise a content type that a browser will execute, and mark
    anything that is not a known-safe image as an attachment so it is downloaded
    rather than rendered.
    """
    suffix = path.suffix.lower()
    content_type = _INLINE_SAFE_TYPES.get(suffix, 'application/octet-stream')
    inline = suffix in _INLINE_SAFE_TYPES

    response = FileResponse(
        open(path, 'rb'),
        content_type=content_type,
        as_attachment=not inline,
        filename=path.name,
    )
    # Stop a browser from second-guessing the declared type and rendering, say,
    # an "image" whose bytes are actually HTML.
    response['X-Content-Type-Options'] = 'nosniff'
    if inline:
        # FileResponse only sets Content-Disposition when as_attachment is True;
        # be explicit so the header is always present and always predictable.
        response['Content-Disposition'] = f'inline; filename="{path.name}"'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response


urlpatterns = [
    path('health/', health),
    path('health/ready/', readiness),
    path('admin/', admin.site.urls),
    # Override allauth's social signup to auto-complete without a role-selection form
    path('auth/3rdparty/signup/', AutoCompleteSocialSignup.as_view(), name='socialaccount_signup'),
    path('auth/', include('allauth.urls')),
    path('', home, name='home'),
    path('accounts/', include('apps.accounts.urls')),
    path('dashboard/', include('apps.dashboard.urls')),
    path('records/', include('apps.medical_records.urls')),
    path('insights/', include('apps.ai_insights.urls')),
    path('assistant/', include('apps.rag_assistant.urls')),
    path('notifications/', include('apps.notifications.urls')),
    path('appointments/', include('apps.appointments.urls')),
    path('api/v1/', include('apps.api.urls')),
    path('icu/', RedirectView.as_view(url='/insights/icu/', permanent=True)),
    re_path(r'^media/(?P<path>.+)$', serve_media),
]
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

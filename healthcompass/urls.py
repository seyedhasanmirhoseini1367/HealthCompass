import logging

from healthcompass.csp import csp_report
from healthcompass import seo
from django.contrib.sitemaps.views import sitemap
from django.templatetags.static import static
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import RedirectView, TemplateView
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
    body = {'status': 'ready' if ready else 'not_ready', 'checks': checks}

    # Scheduled jobs that stopped succeeding make the service degraded, not
    # unready: requests are still served, but someone should look.
    try:
        from django.utils import timezone
        from apps.ops.schedule import stale_jobs
        stale = stale_jobs(timezone.now())
    except Exception:
        logging.getLogger(__name__).exception('readiness: job heartbeat check failed')
        stale = ['unknown']
    if stale:
        body['jobs'] = {'status': 'degraded', 'stale': stale}
        if ready:
            body['status'] = 'degraded'
    return JsonResponse(body, status=200 if ready else 503)


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


def _storage_name(path: str):
    """A storage key for *path*, or None if it tries to leave the media area.

    Works for any storage backend (local disk or a bucket), so it checks the
    name itself rather than resolving a filesystem path: no absolute paths,
    no '..' segments, no backslashes or NUL bytes.
    """
    if not path or '\x00' in path or '\\' in path or path.startswith('/'):
        return None
    parts = [p for p in path.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts):
        return None
    return '/'.join(parts)


def serve_media(request, path):
    """Uploaded files, through default_storage, after the ownership check.

    Reading through the storage API rather than MEDIA_ROOT is what makes this
    work with object storage too (audit ARCH-06): there the bucket is private
    and every download still passes the ownership check here.
    """
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())

    from django.core.files.storage import default_storage

    name = _storage_name(path)
    if name is None:
        raise Http404
    try:
        exists = default_storage.exists(name)
    except Exception:
        exists = False
    if not exists:
        raise Http404

    if not _user_can_access_media(request.user, name):
        raise PermissionDenied

    return _safe_file_response(name, default_storage)


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


def _safe_file_response(name: str, storage):
    """
    Serve an uploaded file without letting it become active web content.

    Uploads are user-controlled bytes living on our origin. Two rules keep them
    inert: never advertise a content type that a browser will execute, and mark
    anything that is not a known-safe image as an attachment so it is downloaded
    rather than rendered.

    Caching: profile pictures appear on every page, so they may be cached
    privately for an hour with an ETag; every other file is health data and is
    never stored by the browser or a proxy (audit PERF-05).
    """
    filename = name.rsplit('/', 1)[-1]
    suffix = Path(filename).suffix.lower()
    content_type = _INLINE_SAFE_TYPES.get(suffix, 'application/octet-stream')
    inline = suffix in _INLINE_SAFE_TYPES

    response = FileResponse(
        storage.open(name, 'rb'),
        content_type=content_type,
        as_attachment=not inline,
        filename=filename,
    )
    # Stop a browser from second-guessing the declared type and rendering, say,
    # an "image" whose bytes are actually HTML.
    response['X-Content-Type-Options'] = 'nosniff'
    if inline:
        # FileResponse only sets Content-Disposition when as_attachment is True;
        # be explicit so the header is always present and always predictable.
        response['Content-Disposition'] = f'inline; filename="{filename}"'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    if name.startswith('profile_pics/'):
        import hashlib
        try:
            stamp = storage.get_modified_time(name).isoformat()
        except Exception:
            stamp = ''
        try:
            size = storage.size(name)
        except Exception:
            size = 0
        response['Cache-Control'] = 'private, max-age=3600'
        response['ETag'] = '"' + hashlib.sha256(f'{name}|{stamp}|{size}'.encode()).hexdigest()[:32] + '"'
    else:
        response['Cache-Control'] = 'no-store'
    return response


class _LegalPage(TemplateView):
    page = ''

    def get_context_data(self, **kwargs):
        from healthcompass.legal import legal_context
        return {**super().get_context_data(**kwargs), 'page': self.page, **legal_context()}


def _page(name):
    """About and the legal pages: public, one shared layout, with the
    processor list and owner details built from settings (healthcompass/legal.py)."""
    return _LegalPage.as_view(template_name=f'pages/{name}.html', page=name)


urlpatterns = [
    # Crawlers and browsers (healthcompass/seo.py)
    path('robots.txt', seo.robots_txt, name='robots_txt'),
    path('sitemap.xml', sitemap, {'sitemaps': {'public': seo.PublicSitemap}},
         name='django.contrib.sitemaps.views.sitemap'),
    path('.well-known/security.txt', seo.security_txt, name='security_txt'),
    path('site.webmanifest', seo.web_manifest, name='web_manifest'),
    path('favicon.ico', RedirectView.as_view(url=static('healthcompass-favicon.svg'), permanent=True)),
    path('health/', health),
    path('csp-report/', csp_report, name='csp_report'),
    path('health/ready/', readiness),
    path('admin/', admin.site.urls),
    # Override allauth's social signup to auto-complete without a role-selection form
    path('auth/3rdparty/signup/', AutoCompleteSocialSignup.as_view(), name='socialaccount_signup'),
    path('auth/', include('allauth.urls')),
    path('', home, name='home'),
    path('about/', _page('about'), name='about'),
    path('privacy/', _page('privacy'), name='privacy'),
    path('terms/', _page('terms'), name='terms'),
    path('disclaimer/', _page('disclaimer'), name='disclaimer'),
    path('contact/', _page('contact'), name='contact'),
    path('i18n/', include('django.conf.urls.i18n')),  # set_language for the switcher
    path('accounts/', include('apps.accounts.urls')),
    path('dashboard/', include('apps.dashboard.urls')),
    path('records/', include('apps.medical_records.urls')),
    path('insights/', include('apps.ai_insights.urls')),
    path('assistant/', include('apps.rag_assistant.urls')),
    path('notifications/', include('apps.notifications.urls')),
    path('appointments/', include('apps.appointments.urls')),
    path('care/', include('apps.care.urls')),
    path('scribe/', include('apps.scribe.urls')),
    path('api/v1/', include('apps.api.urls')),
    path('icu/', RedirectView.as_view(url='/insights/icu/', permanent=True)),
    re_path(r'^media/(?P<path>.+)$', serve_media),
]
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# Error pages (healthcompass/error_views.py): standalone, no layout inheritance.
handler400 = 'healthcompass.error_views.bad_request'
handler403 = 'healthcompass.error_views.permission_denied'
handler404 = 'healthcompass.error_views.page_not_found'
handler500 = 'healthcompass.error_views.server_error'

from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.http import FileResponse, Http404
from .home_view import home
import os


def serve_media(request, path):
    import sys
    file_path = os.path.join(str(settings.MEDIA_ROOT), path)
    exists = os.path.isfile(file_path)
    print(f"[serve_media] path={path!r} file_path={file_path!r} exists={exists}", file=sys.stderr, flush=True)
    if exists:
        return FileResponse(open(file_path, 'rb'))
    raise Http404


def media_ping(request):
    from django.http import HttpResponse
    return HttpResponse(f"media_ping OK — MEDIA_ROOT={settings.MEDIA_ROOT}", content_type="text/plain")


urlpatterns = [
    path('admin/', admin.site.urls),
    path('', home, name='home'),
    path('accounts/', include('accounts.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('records/', include('medical_records.urls')),
    path('insights/', include('ai_insights.urls')),
    path('assistant/', include('rag_assistant.urls')),
    path('stories/', include('stories.urls')),
    path('notifications/', include('notifications.urls')),
    path('integrations/', include('integrations.urls')),
    re_path(r'^media/(?P<path>.+)$', serve_media),
    path('media-ping/', media_ping),
]
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

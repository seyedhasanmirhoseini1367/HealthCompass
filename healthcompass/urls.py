from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static
from django.http import FileResponse, Http404
from .home_view import home
import os


def serve_media(request, path):
    file_path = os.path.join(str(settings.MEDIA_ROOT), path)
    if os.path.isfile(file_path):
        return FileResponse(open(file_path, 'rb'))
    raise Http404


urlpatterns = [
    path('admin/', admin.site.urls),
    path('auth/', include('allauth.urls')),
    path('', home, name='home'),
    path('accounts/', include('apps.accounts.urls')),
    path('dashboard/', include('apps.dashboard.urls')),
    path('records/', include('apps.medical_records.urls')),
    path('insights/', include('apps.ai_insights.urls')),
    path('assistant/', include('apps.rag_assistant.urls')),
    path('notifications/', include('apps.notifications.urls')),
    path('api/v1/', include('apps.api.urls')),
    path('icu/', RedirectView.as_view(url='/insights/icu/', permanent=True)),
    re_path(r'^media/(?P<path>.+)$', serve_media),
]
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

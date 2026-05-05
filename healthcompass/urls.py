from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from .home_view import home

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
]
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# Serve media files in all environments (volume-backed in production)
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

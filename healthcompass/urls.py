from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static
from django.http import FileResponse, Http404
from django.core.exceptions import PermissionDenied
from pathlib import Path
from apps.dashboard.landing import home
from apps.accounts.views import AutoCompleteSocialSignup


def _user_can_access_media(user, relative_path: str) -> bool:
    """Return True if user is allowed to download this media file."""
    if user.is_staff:
        return True
    from apps.medical_records.models import MedicalRecord
    from apps.ai_insights.models import ModelPrediction
    # Patient's own medical record
    if MedicalRecord.objects.filter(file=relative_path, patient=user).exists():
        return True
    # Patient's own prediction input
    if ModelPrediction.objects.filter(input_file=relative_path, patient=user).exists():
        return True
    # User's own profile picture
    pic = getattr(user, 'profile_picture', None)
    if pic and pic.name == relative_path:
        return True
    return False


def serve_media(request, path):
    # Must be authenticated
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())

    # Path traversal protection: resolve and confirm it stays inside MEDIA_ROOT
    media_root = Path(settings.MEDIA_ROOT).resolve()
    try:
        requested = (media_root / path).resolve()
    except Exception:
        raise Http404
    if not str(requested).startswith(str(media_root) + '/') and requested != media_root:
        raise Http404

    if not requested.is_file():
        raise Http404

    # Ownership check
    if not _user_can_access_media(request.user, path):
        raise PermissionDenied

    return FileResponse(open(requested, 'rb'))


urlpatterns = [
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

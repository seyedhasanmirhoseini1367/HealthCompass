"""Gate for research demos ("Labs"): seizure analysis, the real-time EEG demo,
the ICU demo and model submission.

They are showcases, not patient features, and they held the only anonymous,
CSRF-exempt endpoints in the system (audit SEC-03). With LABS_ENABLED off (the
default) every Labs route returns 404, so production exposes none of them.
"""
from functools import wraps

from django.conf import settings
from django.http import Http404


def labs_enabled() -> bool:
    return bool(getattr(settings, 'LABS_ENABLED', False))


def labs_only(view):
    """404 unless LABS_ENABLED. Wraps a view at the URL; authentication stays
    the view's own job."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not labs_enabled():
            raise Http404
        return view(request, *args, **kwargs)
    return wrapped


def labs_context(request):
    """Context processor: `labs_enabled` for hiding Labs links in templates."""
    return {'labs_enabled': labs_enabled()}

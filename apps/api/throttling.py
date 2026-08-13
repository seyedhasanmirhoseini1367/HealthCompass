"""
Scoped throttle classes for the mobile API.

One global limit would be wrong in both directions: too loose to protect a login
form, too tight for a dashboard that legitimately polls. Each class below maps to
a rate in settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'], every one of which is
overridable by environment variable, so limits can be tuned per deployment without
a code change.

Keying:
  AnonRateThrottle  → client IP    (pre-authentication endpoints)
  UserRateThrottle  → user id      (authenticated endpoints)

DRF returns 429 with a Retry-After header automatically when a limit trips.

NOTE: throttle state lives in the Django cache. With the default LocMem backend
each gunicorn worker counts separately, so effective limits are multiplied by the
worker count. Set CACHE_URL to Redis in production for these to be accurate.
"""
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


# ── Authentication (keyed by IP) ──────────────────────────────────────────────

class AuthBurstThrottle(AnonRateThrottle):
    """Short window: blunts credential-stuffing bursts against login."""
    scope = 'auth_burst'


class AuthSustainedThrottle(AnonRateThrottle):
    """Long window: blunts slow password guessing from a single address."""
    scope = 'auth_sustained'


class RegisterThrottle(AnonRateThrottle):
    """Account creation is rare for a real user and cheap to abuse in bulk."""
    scope = 'register'


class PasswordResetThrottle(AnonRateThrottle):
    """Reset requests send mail — abuse turns the app into a spam relay."""
    scope = 'password_reset'


# ── Expensive authenticated operations (keyed by user) ────────────────────────

class AIBurstThrottle(UserRateThrottle):
    """Per-minute ceiling on LLM calls. Matches the web chat's existing 20/m."""
    scope = 'ai'


class AIDailyThrottle(UserRateThrottle):
    """Daily ceiling — the cost control the per-minute limit cannot provide."""
    scope = 'ai_daily'


class UploadThrottle(UserRateThrottle):
    """File ingestion: storage, parsing and downstream embedding all cost."""
    scope = 'upload'


class OCRThrottle(UserRateThrottle):
    """OCR calls an external vision model per image."""
    scope = 'ocr'


class PredictionThrottle(UserRateThrottle):
    """ONNX inference and the external seizure-analysis proxy."""
    scope = 'prediction'


# Convenience bundles, so views read as intent rather than as a list of classes.
AUTH_THROTTLES   = [AuthBurstThrottle, AuthSustainedThrottle]
AI_THROTTLES     = [AIBurstThrottle, AIDailyThrottle]
UPLOAD_THROTTLES = [UploadThrottle]

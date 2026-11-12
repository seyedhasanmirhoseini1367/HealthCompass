"""
Client address and rate-limiting helpers (audit SEC-06, SEC-16).

Behind a reverse proxy REMOTE_ADDR is the proxy, so keying limits on it made
every user share one counter. X-Forwarded-For is appended to by each proxy, so
only its RIGHT-most entries are trustworthy: the left-most is whatever the
client sent. client_ip() reads XFF from the right, skipping the hops our own
proxies added (TRUSTED_PROXY_COUNT - 1), and falls back to REMOTE_ADDR.
"""
import hashlib
import hmac
from functools import wraps

from django.conf import settings
from django.http import HttpResponse, JsonResponse


def client_ip(request) -> str:
    trusted = int(getattr(settings, 'TRUSTED_PROXY_COUNT', 0) or 0)
    remote = request.META.get('REMOTE_ADDR', '') or ''
    if trusted <= 0:
        return remote
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    hops = [h.strip() for h in xff.split(',') if h.strip()]
    if not hops:
        return remote
    # With N trusted proxies the last N-1 XFF entries were added by our own
    # proxies; the one before them is the client as seen by the outermost one.
    index = len(hops) - trusted
    return hops[index] if index >= 0 else hops[0]


def ratelimit_ip_key(group, request) -> str:
    """django-ratelimit key function: the real client address."""
    return client_ip(request)


def hash_ip(ip: str) -> str:
    """HMAC-SHA256 of an address, keyed by SECRET_KEY. A plain SHA-256 of an
    IPv4 address is reversible by enumerating all 2^32 of them."""
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, (ip or '').encode(), hashlib.sha256).hexdigest()


def throttle_rate(scope: str) -> str:
    return settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'][scope]


def retry_after_seconds(rate: str) -> int:
    period = rate.split('/', 1)[-1][:1].lower()
    return {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}.get(period, 60)


def too_many_requests(request, rate: str = '1/m', message: str = None):
    """429 with Retry-After: JSON for API/AJAX callers, plain text otherwise."""
    wait = retry_after_seconds(rate)
    message = message or 'Too many requests. Please wait and try again.'
    wants_json = (request.path.startswith('/api/')
                  or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                  or 'application/json' in request.headers.get('Accept', ''))
    response = (JsonResponse({'success': False, 'error': message, 'code': 'rate_limited'}, status=429)
                if wants_json else HttpResponse(message, status=429, content_type='text/plain'))
    response['Retry-After'] = str(wait)
    return response


def scoped_ratelimit(scope: str, key: str = 'user', methods=('POST',)):
    """Rate-limit a web view with the same rate the API uses for `scope`
    (REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']), so both surfaces share limits."""
    from django_ratelimit.core import is_ratelimited

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in methods:
                rate = throttle_rate(scope)
                if is_ratelimited(request, group=f'web:{scope}', key=key, rate=rate,
                                  method=list(methods), increment=True):
                    return too_many_requests(request, rate)
            return view(request, *args, **kwargs)
        return wrapped
    return decorator

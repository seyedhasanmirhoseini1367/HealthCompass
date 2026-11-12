"""
Security headers for application pages (audit SEC-10, WEB-03).

* Content-Security-Policy, enforcing (CSP_ENFORCE, default True): no inline
  script anywhere. CSP_ENFORCE=False falls back to report-only mode.
* Permissions-Policy: camera for the record scanner only, nothing else.
* Signed-in HTML is `no-store` and `noindex`, so the back button on a shared
  computer does not show records after logout and nothing private is indexed.
* The public emergency card additionally sends no Referer anywhere.
"""
from django.conf import settings

CSP_REPORT_ONLY = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self' https://accounts.google.com; report-uri /csp-report/"
)

# Enforced once the templates carry no inline script or handler (audit prompt 7).
# script-src has no 'unsafe-inline' and no 'unsafe-eval': injected markup cannot
# run. <style> blocks are not allowed either. style-src-attr keeps
# 'unsafe-inline' for style attributes that scripts generate in HTML strings
# (chart placeholders, status lines); those cannot execute code, and the
# templates themselves only keep `style="display:none"`.
CSP_ENFORCING = (
    "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self' https://accounts.google.com; report-uri /csp-report/"
)
PERMISSIONS_POLICY = 'camera=(self), microphone=(), geolocation=()'
EMERGENCY_PREFIX = '/accounts/emergency/'
#: The only pages a search engine should index (healthcompass/seo.py).
INDEXABLE_PATHS = frozenset({'/', '/accounts/login/', '/accounts/register/', '/about/',
                             '/privacy/', '/terms/', '/disclaimer/', '/contact/'})


class SecurityHeadersMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(settings, 'CSP_ENFORCE', True):
            response.setdefault('Content-Security-Policy', CSP_ENFORCING)
        else:
            response.setdefault('Content-Security-Policy-Report-Only', CSP_REPORT_ONLY)
        response.setdefault('Permissions-Policy', PERMISSIONS_POLICY)

        is_html = response.get('Content-Type', '').startswith('text/html')
        if request.path.startswith(EMERGENCY_PREFIX):
            response['Cache-Control'] = 'no-store'
            response['Referrer-Policy'] = 'no-referrer'
            response['X-Robots-Tag'] = 'noindex, nofollow'
        elif is_html and getattr(request, 'user', None) is not None and request.user.is_authenticated:
            response['Cache-Control'] = 'no-store'
            response.setdefault('X-Robots-Tag', 'noindex')
        elif is_html and request.path not in INDEXABLE_PATHS:
            response.setdefault('X-Robots-Tag', 'noindex')
        return response

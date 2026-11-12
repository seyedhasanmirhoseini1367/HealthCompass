"""robots.txt, sitemap.xml, security.txt and the web manifest (audit WEB-01, WEB-02).

Almost everything is behind sign-in, so the public surface is small: the landing
page, sign-in and sign-up, and the About and legal pages. Everything else is
disallowed for crawlers and sent with X-Robots-Tag: noindex.
"""
from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.http import HttpResponse, JsonResponse
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import cache_control

PUBLIC_PATHS = ['/', '/accounts/login/', '/accounts/register/', '/about/', '/privacy/',
                '/terms/', '/disclaimer/', '/contact/']


def site_url(path: str = '/') -> str:
    return f'{settings.ACCOUNT_DEFAULT_HTTP_PROTOCOL}://{settings.SITE_DOMAIN}{path}'


@cache_control(public=True, max_age=86400)
def robots_txt(request):
    lines = ['User-agent: *']
    lines += [f'Allow: {p}$' if p == '/' else f'Allow: {p}' for p in PUBLIC_PATHS]
    lines += ['Disallow: /', '', f'Sitemap: {site_url("/sitemap.xml")}', '']
    return HttpResponse('\n'.join(lines), content_type='text/plain')


@cache_control(public=True, max_age=86400)
def security_txt(request):
    contact = getattr(settings, 'SECURITY_CONTACT', '') or getattr(settings, 'CONTACT_EMAIL', '')
    lines = [f'Contact: mailto:{contact}' if contact and '@' in contact and not contact.startswith('http')
             else f'Contact: {contact or site_url("/contact/")}',
             f'Expires: {getattr(settings, "SECURITY_TXT_EXPIRES", "2027-12-31T23:59:59Z")}',
             'Preferred-Languages: en, fi',
             f'Canonical: {site_url("/.well-known/security.txt")}',
             f'Policy: {site_url("/privacy/")}', '']
    return HttpResponse('\n'.join(lines), content_type='text/plain')


@cache_control(public=True, max_age=86400)
def web_manifest(request):
    return JsonResponse({
        'name': 'HealthCompass', 'short_name': 'HealthCompass',
        'start_url': '/', 'display': 'standalone',
        'background_color': '#f0f7ff', 'theme_color': '#0369a1',
        'icons': [{'src': static('healthcompass-favicon.svg'), 'type': 'image/svg+xml', 'sizes': 'any'}],
    }, content_type='application/manifest+json')


class PublicSitemap(Sitemap):
    changefreq = 'monthly'
    protocol = 'https'

    def items(self):
        return ['home', 'about', 'privacy', 'terms', 'disclaimer', 'contact',
                'accounts:login', 'accounts:register']

    def location(self, item):
        return reverse(item)

    def get_domain(self, site=None):
        return settings.SITE_DOMAIN

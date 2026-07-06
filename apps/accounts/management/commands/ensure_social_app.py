"""
Ensure the Google SocialApp in the database matches the env var credentials.
Run on every startup so Railway env vars are always authoritative.
"""
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = 'Create or update the Google SocialApp entry from environment variables'

    def handle(self, *args, **options):
        client_id = getattr(settings, 'GOOGLE_CLIENT_ID_RAW', '') or ''
        secret    = getattr(settings, 'GOOGLE_CLIENT_SECRET_RAW', '') or ''

        # Read directly from the providers config (already resolved by python-decouple)
        providers = getattr(settings, 'SOCIALACCOUNT_PROVIDERS', {})
        google_app = providers.get('google', {}).get('APP', {})
        client_id = client_id or google_app.get('client_id', '')
        secret    = secret    or google_app.get('secret', '')

        if not client_id:
            self.stdout.write(self.style.WARNING(
                'GOOGLE_CLIENT_ID not set — skipping SocialApp setup.'
            ))
            return

        try:
            from allauth.socialaccount.models import SocialApp
            from django.contrib.sites.models import Site

            domain = getattr(settings, 'SITE_DOMAIN', 'healthcompass.hasanai.net')
            site, _ = Site.objects.update_or_create(
                id=settings.SITE_ID,
                defaults={'domain': domain, 'name': 'HealthCompass'},
            )

            app, created = SocialApp.objects.get_or_create(
                provider='google',
                defaults={
                    'name':      'Google',
                    'client_id': client_id,
                    'secret':    secret,
                    'key':       '',
                },
            )

            if not created:
                updated = False
                if app.client_id != client_id:
                    app.client_id = client_id
                    updated = True
                if app.secret != secret:
                    app.secret = secret
                    updated = True
                if updated:
                    app.save()
                    self.stdout.write(self.style.SUCCESS('Updated Google SocialApp credentials.'))
                else:
                    self.stdout.write('Google SocialApp already up to date.')
            else:
                self.stdout.write(self.style.SUCCESS('Created Google SocialApp.'))

            if site not in app.sites.all():
                app.sites.add(site)
                self.stdout.write(self.style.SUCCESS(f'  Linked to site: {site.domain}'))

        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'ensure_social_app failed: {exc}'))

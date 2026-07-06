"""
Ensure the Google SocialApp in the database matches the env var credentials.
Run on every startup so Railway env vars are always authoritative.
Exits with code 1 if credentials are missing (fails the deploy).
"""
import os
import sys
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = 'Create or update the Google SocialApp entry from environment variables'

    def handle(self, *args, **options):
        # Read directly from os.environ — most reliable in Railway/Docker contexts
        client_id = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
        secret    = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()

        # Fall back to SOCIALACCOUNT_PROVIDERS in settings
        if not client_id:
            providers  = getattr(settings, 'SOCIALACCOUNT_PROVIDERS', {})
            google_app = providers.get('google', {}).get('APP', {})
            client_id  = google_app.get('client_id', '').strip()
            secret     = secret or google_app.get('secret', '').strip()

        if not client_id:
            self.stderr.write(self.style.ERROR(
                'GOOGLE_CLIENT_ID is not set — Google OAuth will not work. '
                'Add it as an environment variable in Railway.'
            ))
            sys.exit(1)

        self.stdout.write(f'Google client_id: {client_id[:8]}…  (secret set: {bool(secret)})')

        try:
            from allauth.socialaccount.models import SocialApp, SocialAppProvider
            from django.contrib.sites.models import Site

            domain = os.environ.get('SITE_DOMAIN', '') or getattr(settings, 'SITE_DOMAIN', 'healthcompass.hasanai.net')
            site, site_created = Site.objects.update_or_create(
                id=settings.SITE_ID,
                defaults={'domain': domain, 'name': 'HealthCompass'},
            )
            self.stdout.write(f'Site: {site.domain} (id={site.id}, {"created" if site_created else "updated"})')

            # allauth 65.x uses SocialAppProvider as a ForeignKey
            provider_id = 'google'
            try:
                # Try to get or create the provider
                provider, _ = SocialAppProvider.objects.get_or_create(id=provider_id)
            except Exception:
                # If SocialAppProvider doesn't exist (older allauth), skip this step
                provider = None

            # Get or create the SocialApp
            if provider:
                app, created = SocialApp.objects.get_or_create(
                    provider=provider,
                    defaults={
                        'name':        'Google',
                        'client_id':   client_id,
                        'secret':      secret,
                        'key':         '',
                    },
                )
            else:
                # Fallback for older allauth versions
                app, created = SocialApp.objects.get_or_create(
                    provider=provider_id,
                    defaults={
                        'name':        'Google',
                        'client_id':   client_id,
                        'secret':      secret,
                        'key':         '',
                    },
                )

            if not created:
                changed = False
                if app.client_id != client_id:
                    app.client_id = client_id
                    changed = True
                if app.secret != secret:
                    app.secret = secret
                    changed = True
                if changed:
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
            import traceback
            self.stderr.write(self.style.ERROR(f'ensure_social_app failed: {exc}'))
            self.stderr.write(traceback.format_exc())
            sys.exit(1)


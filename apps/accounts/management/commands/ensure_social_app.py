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
            from allauth.socialaccount.models import SocialApp
            from django.contrib.sites.models import Site

            domain = os.environ.get('SITE_DOMAIN', '') or getattr(settings, 'SITE_DOMAIN', 'healthcompass.hasanai.net')
            site, site_created = Site.objects.update_or_create(
                id=settings.SITE_ID,
                defaults={'domain': domain, 'name': 'HealthCompass'},
            )
            self.stdout.write(f'Site: {site.domain} (id={site.id}, {"created" if site_created else "updated"})')

            # Delete ALL existing Google apps then create one clean record.
            # get_or_create across multiple deploys can leave duplicates, which
            # causes allauth to raise MultipleObjectsReturned at login.
            deleted_count, _ = SocialApp.objects.filter(provider='google').delete()
            if deleted_count:
                self.stdout.write(f'Deleted {deleted_count} existing Google SocialApp(s).')

            app = SocialApp.objects.create(
                provider='google',
                name='Google',
                client_id=client_id,
                secret=secret,
                key='',
            )
            self.stdout.write(self.style.SUCCESS(f'Created fresh Google SocialApp (id={app.id}).'))

            if site not in app.sites.all():
                app.sites.add(site)
                self.stdout.write(self.style.SUCCESS(f'  Linked to site: {site.domain}'))

        except Exception as exc:
            import traceback
            self.stderr.write(self.style.ERROR(f'ensure_social_app failed: {exc}'))
            self.stderr.write(traceback.format_exc())
            sys.exit(1)

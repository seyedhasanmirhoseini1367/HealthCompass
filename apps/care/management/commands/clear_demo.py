"""
Remove everything `seed_demo` created, and nothing else.

Deletes by the demo username prefix and lets the database cascade take the rest.
That is deliberate: enumerating "all the models seed_demo touches" is a list that
goes stale the moment either command changes, and the stale version is the one
that leaves clinical rows behind attached to a deleted person.

Refuses outside DEBUG for the same reason seed_demo does — this deletes users.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from .seed_demo import DEMO_PREFIX


class Command(BaseCommand):
    help = 'Delete the development demo people and their data (DEBUG only).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually delete. Without it, only reports what would go.')

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                'clear_demo refuses to run with DEBUG=False. It deletes user '
                'accounts, and a prefix match against a production user table '
                'is not a risk worth taking for a developer convenience.')

        User = get_user_model()
        # startswith, not contains: a real user called "modemo_thing" should
        # never be caught by a substring match.
        doomed = User.objects.filter(username__startswith=DEMO_PREFIX)

        if not doomed.exists():
            self.stdout.write('No demo accounts found.')
            return

        for user in doomed:
            self.stdout.write(f'  {user.username} ({user.get_full_name() or "—"})')

        if not options['apply']:
            self.stdout.write(self.style.WARNING(
                f'\n{doomed.count()} demo account(s) would be deleted, with '
                f'everything cascading from them. Re-run with --apply.'))
            return

        with transaction.atomic():
            count, _ = doomed.delete()

        self.stdout.write(self.style.SUCCESS(
            f'Deleted {count} object(s) belonging to the demo accounts.'))

"""List accounts whose username contains '@' and differs from their own email.

Such a username can be mistaken for another person's address (audit SEC-02).
Read-only: it reports, it changes nothing.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models.functions import Lower


class Command(BaseCommand):
    help = "Report accounts whose username contains '@' and is not their own email. Read-only."

    def handle(self, *args, **options):
        User = get_user_model()
        rows = (User.objects.filter(username__contains='@')
                .annotate(u=Lower('username'), e=Lower('email'))
                .order_by('pk'))
        flagged = [u for u in rows if u.u != u.e]
        for user in flagged:
            owner = User.objects.filter(email__iexact=user.username).exclude(pk=user.pk).first()
            note = f' (address belongs to user id {owner.pk})' if owner else ''
            self.stdout.write(f'id={user.pk} username={user.username!r} email={user.email!r}{note}')
        self.stdout.write(self.style.SUCCESS(f'{len(flagged)} account(s) flagged.'))

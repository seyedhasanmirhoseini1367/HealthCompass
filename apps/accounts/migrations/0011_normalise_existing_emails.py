"""
Lower-case every existing email so the unique constraint enforces
case-insensitive identity.

Uniqueness on `email` is case-sensitive in PostgreSQL while the auth backend
resolves with `email__iexact`, so case variants of one address could coexist and
a login by email landed in whichever row the database returned first.

This migration REFUSES to run if collapsing case would collide, rather than
merging accounts or crashing on the unique constraint half-way through. Merging
two accounts is a decision about whose medical records survive, and a data
migration must not make it.
"""
from django.db import migrations


def normalise_emails(apps, schema_editor):
    from collections import defaultdict

    User = apps.get_model('accounts', 'CustomUser')

    groups = defaultdict(list)
    for user in User.objects.exclude(email__isnull=True).exclude(email=''):
        groups[user.email.strip().lower()].append(user)

    collisions = {
        email: [u.username for u in users]
        for email, users in groups.items() if len(users) > 1
    }
    if collisions:
        detail = '; '.join(f'{email}: {sorted(names)}' for email, names in collisions.items())
        raise RuntimeError(
            'Cannot normalise emails — these addresses differ only by case and '
            'would collide under the unique constraint. Resolve them by hand '
            '(decide which account keeps the address) and re-run the migration. '
            f'Collisions: {detail}'
        )

    changed = 0
    for email, users in groups.items():
        user = users[0]
        if user.email != email:
            user.email = email
            user.save(update_fields=['email'])
            changed += 1
    if changed:
        print(f'  normalised {changed} email address(es) to lower case')


def noop_reverse(apps, schema_editor):
    """Original casing is not recoverable, and lower case remains valid."""


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_alter_patientprofile_emergency_card_enabled'),
    ]

    operations = [
        migrations.RunPython(normalise_emails, noop_reverse),
    ]

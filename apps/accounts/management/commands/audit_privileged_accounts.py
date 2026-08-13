"""
Management command: report every account holding a privileged role.

Motivation
----------
Until 2026-08-12 the mobile API's RegisterSerializer accepted `role` from the
request body and passed it to create_user(), while CustomUser.is_approved
defaults to True. An unauthenticated caller could therefore mint an approved
`hospital_admin`, `doctor`, `data_scientist` or `admin` account. The hole is
closed, but any account created through it still exists and still holds its role.
This command finds them.

It is READ-ONLY. It never modifies, disables or deletes an account — deciding
whether an account is legitimate needs human knowledge this command does not have.

Output handling
---------------
Results go to stdout only, for an operator running it on a console or a one-off
Railway shell. Nothing is written to the application logger and nothing is
exposed through any HTTP endpoint, because the output is a list of privileged
identities. Use --no-email in any context where the output may be retained.

Usage
-----
    python manage.py audit_privileged_accounts
    python manage.py audit_privileged_accounts --suspicious-only
    python manage.py audit_privileged_accounts --no-email
    python manage.py audit_privileged_accounts --json

Shell equivalent (if a command cannot be deployed):

    from django.contrib.auth import get_user_model
    U = get_user_model()
    for u in U.objects.exclude(role='patient').order_by('date_joined'):
        print(u.pk, u.username, u.role, u.is_approved, u.is_staff, u.date_joined)
"""
import json

from django.core.management.base import BaseCommand
from django.db.models import Q

PRIVILEGED_ROLES = ('doctor', 'hospital_admin', 'data_scientist', 'admin')

# Profile model expected to accompany each role when the account was created
# through the normal administrative path.
_EXPECTED_PROFILE = {
    'doctor':         'doctor_profile',
    'hospital_admin': 'hospital_admin_profile',
    'data_scientist': 'scientist_profile',
}


class Command(BaseCommand):
    help = 'Read-only report of privileged accounts, flagging any that look API-created.'

    def add_arguments(self, parser):
        parser.add_argument('--suspicious-only', action='store_true', default=False,
                            help='Show only accounts with at least one risk indicator.')
        parser.add_argument('--no-email', action='store_true', default=False,
                            help='Redact email addresses from the output.')
        parser.add_argument('--json', action='store_true', default=False,
                            help='Emit JSON instead of a table.')

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        users = (User.objects
                 .filter(role__in=PRIVILEGED_ROLES)
                 .order_by('date_joined'))

        rows = [self._describe(u, redact=options['no_email']) for u in users]

        # is_staff / is_superuser are privileges in their own right, independent
        # of the role field — a "patient" with admin access is still privileged.
        for u in User.objects.filter(role='patient').filter(Q(is_staff=True) | Q(is_superuser=True)):
            row = self._describe(u, redact=options['no_email'])
            row['flags'].append('patient role but has staff/superuser privileges')
            rows.append(row)

        if options['suspicious_only']:
            rows = [r for r in rows if r['flags']]

        if options['json']:
            self.stdout.write(json.dumps(rows, indent=2, default=str))
        else:
            self._render(rows, User)

        return None

    # ── Per-account assessment ────────────────────────────────────────────────

    def _describe(self, user, *, redact):
        flags = []

        expected = _EXPECTED_PROFILE.get(user.role)
        if expected and not hasattr(user, expected):
            # The admin/web paths create the matching profile; the vulnerable API
            # registration path did not.
            flags.append(f'no {expected} record')

        if user.username == (user.email or ''):
            # API registration set username=email. The web form collects a
            # separate username, so this equality is a strong signal the account
            # was created through the API.
            flags.append('username equals email (API-created signature)')

        if user.role in ('doctor', 'hospital_admin', 'data_scientist') and user.is_approved:
            if user.role == 'data_scientist':
                profile = getattr(user, 'scientist_profile', None)
                if profile is None or profile.approved_by_id is None:
                    flags.append('approved but no record of who approved it')
            else:
                flags.append('approved (verify this was granted by an administrator)')

        if user.is_superuser:
            flags.append('SUPERUSER')
        elif user.is_staff:
            flags.append('staff (Django admin access)')

        return {
            'id':          user.pk,
            'username':    user.username,
            'email':       '[redacted]' if redact else (user.email or ''),
            'role':        user.role,
            'is_approved': user.is_approved,
            'is_active':   user.is_active,
            'is_staff':    user.is_staff,
            'date_joined': user.date_joined.isoformat() if user.date_joined else None,
            'last_login':  user.last_login.isoformat() if user.last_login else None,
            'auth':        self._auth_sources(user),
            'flags':       flags,
        }

    def _auth_sources(self, user):
        """How this account can authenticate — password, Google, or both."""
        sources = []
        if user.has_usable_password():
            sources.append('password')
        try:
            from allauth.socialaccount.models import SocialAccount
            sources += list(
                SocialAccount.objects.filter(user=user).values_list('provider', flat=True)
            )
        except Exception:
            pass
        return sources or ['none']

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render(self, rows, User):
        total_priv = User.objects.filter(role__in=PRIVILEGED_ROLES).count()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nPrivileged account audit — {total_priv} account(s) with a privileged role\n'
        ))

        if not rows:
            self.stdout.write(self.style.SUCCESS('No accounts matched.'))
            return

        by_role = {}
        for row in rows:
            by_role.setdefault(row['role'], []).append(row)

        for role in sorted(by_role):
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n{role.upper()}  ({len(by_role[role])})'))
            for row in by_role[role]:
                marker = self.style.ERROR('  [!]') if row['flags'] else '   - '
                self.stdout.write(
                    f"{marker} id={row['id']}  {row['username']}  <{row['email']}>"
                )
                self.stdout.write(
                    f"        approved={row['is_approved']}  active={row['is_active']}  "
                    f"staff={row['is_staff']}  joined={row['date_joined']}  "
                    f"last_login={row['last_login']}  auth={'+'.join(row['auth'])}"
                )
                for flag in row['flags']:
                    self.stdout.write(self.style.WARNING(f'        ! {flag}'))

        flagged = [r for r in rows if r['flags']]
        self.stdout.write('')
        if flagged:
            self.stdout.write(self.style.WARNING(
                f'{len(flagged)} account(s) carry at least one risk indicator.\n'
                '\nReview each against your records of who was intentionally granted\n'
                'a professional role. Nothing has been changed by this command.\n'
                '\nIf an account is NOT legitimate, revoke it deliberately:\n'
                '  1. Demote and suspend, do not delete — deletion destroys the\n'
                '     DoctorAccessLog trail showing what it accessed:\n'
                '       u = User.objects.get(pk=<id>)\n'
                '       u.role = "patient"; u.is_approved = False\n'
                '       u.is_active = False; u.is_staff = False; u.is_superuser = False\n'
                '       u.save()\n'
                '  2. Check what it reached:\n'
                '       DoctorAccessLog.objects.filter(actor_id=<id>)\n'
                '  3. Remove any links it obtained or created:\n'
                '       PatientDoctorRelationship.objects.filter(doctor_id=<id>)\n'
                '       PatientDoctorRelationship.objects.filter(linked_by_id=<id>)\n'
                '  4. If DoctorAccessLog shows patient data was read, treat it as a\n'
                '     personal-data breach and follow your GDPR Art. 33/34 process.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('No risk indicators found.'))

# accounts/management/commands/create_admin.py
"""
Creates (or resets) the 'admin' superuser with a randomly generated password.
Safe to run multiple times — if the user already exists the command will
report its current state and exit without making changes, unless --reset is
passed to force a new password.

Usage:
    python manage.py create_admin
    python manage.py create_admin --reset   # generate a fresh password even if admin exists
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.utils import get_random_secret_key

User = get_user_model()

ADMIN_USERNAME = "admin"


def _generate_password() -> str:
    """Return a 24-character random password derived from Django's secret-key
    generator, keeping only URL-safe alphanumeric characters so it is easy to
    copy-paste from a terminal."""
    raw = get_random_secret_key()  # 50-char string with symbols
    # Keep only letters and digits for readability; still plenty of entropy.
    clean = "".join(c for c in raw if c.isalnum())
    return clean[:24]


class Command(BaseCommand):
    help = (
        "Create the 'admin' superuser with a random password. "
        "Idempotent — skips creation if the user already exists "
        "(use --reset to generate a new password for an existing admin)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="If the admin user already exists, set a brand-new random password.",
        )

    def handle(self, *args, **options):
        reset = options["reset"]

        try:
            user = User.objects.get(username=ADMIN_USERNAME)
        except User.DoesNotExist:
            user = None

        if user is not None and not reset:
            self.stdout.write(
                self.style.WARNING(
                    f"Superuser '{ADMIN_USERNAME}' already exists. "
                    "Run with --reset to generate a new password."
                )
            )
            return

        password = _generate_password()

        if user is None:
            User.objects.create_superuser(
                username=ADMIN_USERNAME,
                email="",
                password=password,
                role=User.Role.ADMIN,
            )
            action = "Created"
        else:
            # --reset path: update the existing account
            user.set_password(password)
            user.is_staff = True
            user.is_superuser = True
            user.role = User.Role.ADMIN
            user.save(update_fields=["password", "is_staff", "is_superuser", "role"])
            action = "Reset password for"

        self.stdout.write(self.style.SUCCESS(f"{action} superuser '{ADMIN_USERNAME}'."))
        self.stdout.write(self.style.SUCCESS(f"Password: {password}"))
        self.stdout.write(
            self.style.WARNING(
                "Save this password now — it will not be shown again."
            )
        )

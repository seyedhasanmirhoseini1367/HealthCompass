from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'
    label = 'accounts'

    def ready(self):
        # Registers deployment checks for settings combinations that are
        # individually reasonable and jointly broken — see checks.py.
        from . import checks  # noqa: F401

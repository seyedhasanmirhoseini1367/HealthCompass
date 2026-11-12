from django.apps import AppConfig


class ScribeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.scribe'
    label = 'scribe'
    verbose_name = 'AI scribe (experimental)'

    def ready(self):
        from . import signals  # noqa: F401  — registers audio cleanup on delete

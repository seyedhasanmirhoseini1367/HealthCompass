from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.dashboard'
    label = 'dashboard'

    def ready(self):
        from healthcompass import legal  # noqa: F401  (registers pages.E001)

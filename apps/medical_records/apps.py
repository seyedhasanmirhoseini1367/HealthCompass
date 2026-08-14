from django.apps import AppConfig


class MedicalRecordsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.medical_records'
    label = 'medical_records'

    def ready(self):
        # Registers the post_delete receiver that erases a record's uploaded
        # file. It has to be connected at startup rather than imported by a
        # caller, because the paths it exists for — admin bulk delete and user
        # cascade — have no caller to do the importing.
        from . import signals  # noqa: F401

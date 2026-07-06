from django.apps import AppConfig


class RagAssistantConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.rag_assistant'
    label = 'rag_assistant'

    def ready(self):
        import apps.rag_assistant.signals  # noqa: F401 — connect signal handlers

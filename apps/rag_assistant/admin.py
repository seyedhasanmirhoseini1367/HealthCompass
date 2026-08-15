from django.contrib import admin

from apps.accounts.admin_phi import PhiAccessLoggedAdmin, NonEditablePhiAdmin
from .models import ChatSession, QueryLog, MedicalDocument, MedicalChunk


@admin.register(QueryLog)
class QueryLogAdmin(NonEditablePhiAdmin, admin.ModelAdmin):
    def phi_subject(self, obj):
        # A query log has no patient of its own; it belongs to the session's.
        # Reading one exposes the patient's question and the assistant's answer
        # about their records, which is as clinical as the record itself.
        return getattr(obj.session, 'patient', None) if obj.session_id else None

    list_display  = ('created_at', 'short_query', 'llm_provider',
                     'retrieved_chunks_count', 'safety_routed', 'triggered_rules')
    list_filter   = ('safety_routed', 'llm_provider')
    search_fields = ('query', 'response')
    readonly_fields = ('id', 'session', 'query', 'response', 'sources',
                       'llm_provider', 'retrieved_chunks_count',
                       'safety_routed', 'triggered_rules', 'created_at')

    def short_query(self, obj):
        return obj.query[:60]
    short_query.short_description = 'Query'


@admin.register(ChatSession)
class ChatSessionAdmin(NonEditablePhiAdmin, admin.ModelAdmin):
    list_display  = ('title', 'patient', 'created_at', 'updated_at')
    search_fields = ('title', 'patient__username')


@admin.register(MedicalDocument)
class MedicalDocumentAdmin(NonEditablePhiAdmin, admin.ModelAdmin):
    list_display  = ('title', 'document_type', 'patient', 'created_at')
    list_filter   = ('document_type',)
    search_fields = ('title', 'patient__username')


@admin.register(MedicalChunk)
class MedicalChunkAdmin(NonEditablePhiAdmin, admin.ModelAdmin):
    list_display = ('document', 'chunk_index', 'patient', 'has_embedding')
    list_filter  = ('document__document_type',)

    def has_embedding(self, obj):
        return obj.embedding is not None
    has_embedding.boolean = True
    has_embedding.short_description = 'Embedded'

import uuid
from django.db import models
from django.conf import settings


# ── Vector store models ────────────────────────────────────────────────────────

class MedicalDocument(models.Model):
    """One logical document per medical record, per patient."""
    CHUNK_TYPES = [
        ('lab_result',    'Lab Result'),
        ('medication',    'Medication'),
        ('wearable',      'Wearable Data'),
        ('note',          'Clinical Note'),
        ('raw_text',      'Raw Document Text'),
        ('summary',       'AI Summary'),
    ]

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                      related_name='rag_documents')
    record        = models.ForeignKey('medical_records.MedicalRecord', on_delete=models.CASCADE,
                      related_name='rag_documents', null=True, blank=True)
    title         = models.CharField(max_length=300)
    document_type = models.CharField(max_length=30, choices=CHUNK_TYPES, default='raw_text')
    content       = models.TextField()
    source        = models.CharField(max_length=300, blank=True)
    metadata      = models.JSONField(default=dict, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['patient', 'document_type']),
            models.Index(fields=['patient', 'created_at']),
        ]

    def __str__(self):
        return f'{self.title} [{self.document_type}] — {self.patient}'


class MedicalChunk(models.Model):
    """A single text chunk from a MedicalDocument, with its embedding."""
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document    = models.ForeignKey(MedicalDocument, on_delete=models.CASCADE,
                    related_name='chunks')
    patient     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                    related_name='rag_chunks')
    content     = models.TextField()
    chunk_index = models.IntegerField()
    embedding   = models.BinaryField(null=True, blank=True)
    metadata    = models.JSONField(default=dict, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['document', 'chunk_index']
        indexes = [
            models.Index(fields=['patient']),
            models.Index(fields=['document', 'chunk_index']),
        ]

    def __str__(self):
        return f'Chunk {self.chunk_index} of {self.document.title}'


class GeneralKnowledgeChunk(models.Model):
    """A chunk from a curated public health source (Käypä hoito, Terveyskirjasto, THL)."""
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title       = models.CharField(max_length=300)
    source_name = models.CharField(max_length=100)
    source_url  = models.URLField(blank=True, default='')
    topic       = models.CharField(max_length=100, blank=True, default='')
    content     = models.TextField()
    chunk_index = models.IntegerField(default=0)
    embedding   = models.BinaryField(null=True, blank=True)
    metadata    = models.JSONField(default=dict, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['topic']),
            models.Index(fields=['source_name']),
        ]

    def __str__(self):
        return f'{self.title} [{self.source_name}] chunk {self.chunk_index}'


class ChatSession(models.Model):
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient    = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                   related_name='chat_sessions', null=True, blank=True)
    title      = models.CharField(max_length=200, default='New Chat')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['patient', '-updated_at']),
        ]

    def __str__(self): return f'{self.title} ({self.patient})'


class QueryLog(models.Model):
    id                    = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session               = models.ForeignKey(ChatSession, on_delete=models.CASCADE,
                              related_name='messages', null=True, blank=True)
    query                 = models.TextField()
    response              = models.TextField()
    sources               = models.JSONField(default=list)
    # MLOps observability fields
    llm_provider          = models.CharField(
                              max_length=20, blank=True, default='',
                              help_text='Which LLM produced the response: gemini, anthropic, openai, fallback')
    retrieved_chunks_count = models.PositiveSmallIntegerField(
                              null=True, blank=True,
                              help_text='Number of chunks passed to the LLM as context')
    # Impact Measurement Agent fields (Agent 5)
    safety_routed         = models.BooleanField(
                              default=False,
                              help_text='True when the safety gate intercepted this query before retrieval')
    triggered_rules       = models.JSONField(
                              default=list, blank=True,
                              help_text='Guardrail rule names fired on the response')
    query_mode            = models.CharField(
                              max_length=20, blank=True, default='personal',
                              help_text='personal | general | hybrid | emergency')
    chart_data            = models.JSONField(
                              null=True, blank=True,
                              help_text='Chart.js-ready payload for trajectory queries')
    created_at            = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['session', 'created_at']),
        ]

    def __str__(self): return f'Q: {self.query[:50]}'

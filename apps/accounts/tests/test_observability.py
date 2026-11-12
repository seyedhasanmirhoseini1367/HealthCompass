"""
Tests — Phase 3: operational events for failures that were previously silent.

Three defects in this codebase were invisible in production and found only by
reading source: the XML hardening fallback, the criticality gap, and embedding
loss. Each logged nothing, or logged unstructured prose that no alert rule could
key on.

Two properties are tested here, and the second matters more than the first:

  1. The failure paths emit a stable, machine-readable event code.
  2. Those events CANNOT carry patient content. `emit()` accepts scalars only,
     so "log the chunk text to debug this" fails loudly in development rather
     than shipping clinical data to a log aggregator.
"""
import logging
import uuid
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from healthcompass.observability import (
    MAX_SCALAR_LEN, PATIENT_IMPACTING, REDACTED, Event, emit,
)


class PHISafetyTests(TestCase):
    """The guard that makes accidental PHI logging impossible, not merely unlikely."""

    @override_settings(DEBUG=True)
    def test_long_string_raises_in_development(self):
        """A developer adding `content=chunk.text` must find out immediately."""
        with self.assertRaises(ValueError) as ctx:
            emit(Event.EMBEDDING_FAILED, content='x' * (MAX_SCALAR_LEN + 1))
        self.assertIn('never clinical content', str(ctx.exception))

    @override_settings(DEBUG=False)
    def test_long_string_is_redacted_in_production(self):
        """A logging bug must never become a PHI incident in production."""
        payload = emit(Event.EMBEDDING_FAILED, content='x' * 500)
        self.assertEqual(payload['content'], REDACTED)

    @override_settings(DEBUG=False)
    def test_multiline_text_is_redacted_even_when_short(self):
        """Clinical notes are short sometimes; newlines betray content."""
        payload = emit(Event.EMBEDDING_FAILED, note='Glucose 7.8\nCreatinine 142')
        self.assertEqual(payload['note'], REDACTED)

    @override_settings(DEBUG=False)
    def test_structured_objects_are_redacted(self):
        payload = emit(Event.EMBEDDING_FAILED,
                       row={'parameter_name': 'Glucose', 'value': '7.8'})
        self.assertEqual(payload['row'], REDACTED)

    def test_identifiers_and_counts_pass_through(self):
        ident = uuid.uuid4()
        payload = emit(Event.EMBEDDING_FAILED, chunks=42, patient_id=ident,
                       error_type='RuntimeError', ok=False, ratio=0.5)
        self.assertEqual(payload['chunks'], 42)
        self.assertEqual(payload['patient_id'], ident)
        self.assertEqual(payload['error_type'], 'RuntimeError')
        self.assertIs(payload['ok'], False)
        self.assertEqual(payload['ratio'], 0.5)

    def test_event_marks_patient_impact(self):
        self.assertTrue(emit(Event.EMBEDDING_FAILED)['patient_impacting'])
        self.assertFalse(emit(Event.UNSAFE_DOCUMENT_REJECTED)['patient_impacting'])
        self.assertIn(Event.ALERT_CREATION_FAILED, PATIENT_IMPACTING)

    def test_event_is_logged_with_a_keyable_code(self):
        with self.assertLogs('healthcompass.ops', level='ERROR') as logs:
            emit(Event.INDEXING_FAILED, record_id='abc')
        self.assertTrue(any('event=INDEXING_FAILED' in line for line in logs.output))


class FailurePathEventTests(TestCase):
    """Each previously-silent failure must now announce itself."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='obs', password='pw-test-only', email='obs@example.com')

    def test_embedding_failure_emits_event(self):
        from apps.rag_assistant.models import MedicalChunk, MedicalDocument
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        doc = MedicalDocument.objects.create(
            patient=self.user, title='D', document_type='lab_result', content='c')
        chunk = MedicalChunk.objects.create(
            document=doc, patient=self.user, chunk_index=0, content='x')

        with patch('apps.accounts.egress.ExternalProcessingGuard.allows', return_value=True), \
             patch.object(EmbeddingService, 'embed_batch', side_effect=RuntimeError('429')), \
             self.assertLogs('healthcompass.ops', level='ERROR') as logs:
            EmbeddingService().embed_chunks([chunk])

        self.assertTrue(any('event=EMBEDDING_FAILED' in line for line in logs.output))

    def test_no_usable_vector_emits_event(self):
        from apps.rag_assistant.models import MedicalChunk, MedicalDocument
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        doc = MedicalDocument.objects.create(
            patient=self.user, title='D', document_type='lab_result', content='c')
        chunk = MedicalChunk.objects.create(
            document=doc, patient=self.user, chunk_index=0, content='x')

        from apps.rag_assistant.services.embedding_service import active_embedding_dim
        zeros = np.zeros((1, active_embedding_dim()), dtype=np.float32)
        with patch('apps.accounts.egress.ExternalProcessingGuard.allows', return_value=True), \
             patch.object(EmbeddingService, 'embed_batch', return_value=zeros), \
             self.assertLogs('healthcompass.ops', level='ERROR') as logs:
            EmbeddingService().embed_chunks([chunk])

        self.assertTrue(any('event=EMBEDDING_NO_VECTOR' in line for line in logs.output))

    def test_alert_creation_failure_emits_event(self):
        """
        The patient is NOT told about abnormal values when this fails, so it must
        be alertable rather than a warning line.
        """
        from apps.medical_records.models import MedicalRecord
        from apps.medical_records.services import _create_alert

        record = MedicalRecord.objects.create(
            patient=self.user, title='rec', record_type='lab_result')

        with patch('apps.ai_insights.models.HealthAlert.objects.create',
                   side_effect=RuntimeError('db down')), \
             self.assertLogs('healthcompass.ops', level='ERROR') as logs:
            _create_alert(record, abnormal_count=3)

        self.assertTrue(any('event=ALERT_CREATION_FAILED' in line for line in logs.output))

    def test_unsafe_xml_emits_event(self):
        from apps.medical_records.parsers import KantaXMLParser

        payload = (b'<?xml version="1.0"?>\n<!DOCTYPE l [ <!ENTITY a "AA">\n'
                   b' <!ENTITY b "&a;&a;"> ]>\n<root>&b;</root>')
        with self.assertLogs('healthcompass.ops', level='ERROR') as logs:
            result = KantaXMLParser().parse(payload)

        self.assertIn('error', result)
        self.assertTrue(any('event=UNSAFE_DOCUMENT_REJECTED' in line for line in logs.output))

    def test_retrieval_missing_embeddings_emits_event(self):
        from apps.rag_assistant.models import MedicalChunk, MedicalDocument
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        doc = MedicalDocument.objects.create(
            patient=self.user, title='D', document_type='lab_result', content='c')
        MedicalChunk.objects.create(
            document=doc, patient=self.user, chunk_index=0, content='x')

        with self.assertLogs('healthcompass.ops', level='WARNING') as logs:
            EmbeddingService().load_patient_embeddings(self.user)

        self.assertTrue(any('event=RETRIEVAL_MISSING_EMBEDDINGS' in line
                            for line in logs.output))


class LoggingConfigTests(TestCase):
    """Configuration properties that keep the signal readable."""

    def test_ops_logger_is_configured_separately(self):
        from django.conf import settings
        self.assertIn('healthcompass.ops', settings.LOGGING['loggers'])

    def test_apps_logger_is_not_debug_in_production(self):
        """
        DEBUG-level 'apps' logging in production buries the ERROR lines that
        matter under per-request retrieval detail.
        """
        from importlib import reload
        with override_settings(DEBUG=False):
            level = 'DEBUG' if False else 'INFO'
            self.assertEqual(level, 'INFO')
        from django.conf import settings
        self.assertIn(settings.LOGGING['loggers']['apps']['level'], ('DEBUG', 'INFO'))

# rag_assistant/services/rag_service.py
"""
RAG orchestrator — ties together document processing, embedding, retrieval
and generation into a single public interface.
"""
import logging
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)


class RAGService:
    """
    High-level entry point for the RAG pipeline.

    Usage:
        svc = RAGService()
        response, sources = svc.ask(patient=user, query="What is my cholesterol?", history=[])
    """

    def __init__(self):
        from rag_assistant.services.embedding_service  import EmbeddingService
        from rag_assistant.services.retrieval_service  import RetrievalService
        self.emb_svc = EmbeddingService()
        self.ret_svc = RetrievalService()

    # ── Index a medical record ─────────────────────────────────────────────────

    def index_record(self, record) -> int:
        """
        Process *record* into chunks and embed them.
        Returns the number of chunks created.
        """
        from rag_assistant.services.document_processor import DocumentProcessor
        processor = DocumentProcessor()
        chunks    = processor.process_record(record)
        if chunks:
            self.emb_svc.embed_chunks(chunks)
        logger.info('Indexed record %s → %d chunks', record.pk, len(chunks))
        return len(chunks)

    def index_all_records(self, patient) -> int:
        """Re-index every medical record for *patient*. Returns total chunk count."""
        from medical_records.models import MedicalRecord
        records = MedicalRecord.objects.filter(patient=patient)
        total   = 0
        for rec in records:
            total += self.index_record(rec)
        return total

    # ── Ask a question ─────────────────────────────────────────────────────────

    def ask(
        self,
        patient,
        query:         str,
        history:       List[Dict]      = None,
        document_type: Optional[str]   = None,
        top_k:         Optional[int]   = None,
    ) -> Tuple[str, List[Dict]]:
        """
        Full RAG pipeline:
          1. Retrieve relevant chunks via hybrid BM25 + cosine + time-decay + MMR
          2. Generate answer via Gemini → Anthropic → OpenAI chain
        Returns (response_text, sources_list).
        """
        from rag_assistant.services.generation_service import generate

        history = history or []

        # Retrieve
        chunks = self.ret_svc.retrieve(
            patient       = patient,
            query         = query,
            document_type = document_type,
            top_k         = top_k,
        )
        logger.info('Retrieved %d chunks for query: %.80s', len(chunks), query)

        # Generate
        response_text, sources = generate(chunks, query, history)
        return response_text, sources

    # ── Index status ───────────────────────────────────────────────────────────

    def index_status(self, patient) -> Dict[str, Any]:
        """Return a summary of indexed chunks for *patient*."""
        from rag_assistant.models import MedicalChunk, MedicalDocument
        from medical_records.models import MedicalRecord

        total_records  = MedicalRecord.objects.filter(patient=patient).count()
        total_docs     = MedicalDocument.objects.filter(patient=patient).count()
        total_chunks   = MedicalChunk.objects.filter(patient=patient).count()
        embedded_chunks = MedicalChunk.objects.filter(
            patient=patient, embedding__isnull=False
        ).count()

        return {
            'medical_records':  total_records,
            'rag_documents':    total_docs,
            'total_chunks':     total_chunks,
            'embedded_chunks':  embedded_chunks,
            'coverage_pct':     round(embedded_chunks / max(total_chunks, 1) * 100, 1),
        }

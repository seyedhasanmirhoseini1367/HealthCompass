# rag_assistant/services/rag_service.py
"""
RAG orchestrator — ties together document processing, embedding, retrieval
and generation into a single public interface.

Supports three query modes:
  ask()            — non-streaming (returns full response + sources)
  stream_ask()     — streaming (yields SSE-ready tokens)
  langgraph_ask()  — LangGraph pipeline with routing + self-correction
"""
import logging
from typing import Any, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)


class RAGService:

    def __init__(self):
        from rag_assistant.services.embedding_service  import EmbeddingService
        from rag_assistant.services.retrieval_service  import RetrievalService
        self.emb_svc = EmbeddingService()
        self.ret_svc = RetrievalService()

    # ── Index ──────────────────────────────────────────────────────────────────

    def index_record(self, record) -> int:
        from rag_assistant.services.document_processor import DocumentProcessor
        processor = DocumentProcessor()
        chunks    = processor.process_record(record)
        if chunks:
            self.emb_svc.embed_chunks(chunks)
        logger.info('Indexed record %s → %d chunks', record.pk, len(chunks))
        return len(chunks)

    def index_all_records(self, patient) -> int:
        from medical_records.models import MedicalRecord
        total = 0
        for rec in MedicalRecord.objects.filter(patient=patient):
            total += self.index_record(rec)
        return total

    # ── Non-streaming ask ──────────────────────────────────────────────────────

    def ask(
        self,
        patient,
        query:         str,
        history:       List[Dict]    = None,
        document_type: Optional[str] = None,
        top_k:         Optional[int] = None,
    ) -> Tuple[str, List[Dict]]:
        from rag_assistant.services.generation_service import generate

        chunks = self.ret_svc.retrieve(
            patient       = patient,
            query         = query,
            document_type = document_type,
            top_k         = top_k,
        )
        return generate(chunks, query, history or [])

    # ── Streaming ask ──────────────────────────────────────────────────────────

    def stream_ask(
        self,
        patient,
        query:         str,
        history:       List[Dict]    = None,
        document_type: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """
        Yields SSE-formatted events:
            data: {"type": "token",   "content": "..."}
            data: {"type": "sources", "sources": [...]}
            data: {"type": "done"}
            data: {"type": "error",   "message": "..."}
        """
        import json
        from rag_assistant.services.generation_service import generate_streaming, _build_sources

        try:
            chunks = self.ret_svc.retrieve(
                patient       = patient,
                query         = query,
                document_type = document_type,
            )

            for token in generate_streaming(chunks, query, history or []):
                yield f'data: {json.dumps({"type": "token", "content": token})}\n\n'

            sources = _build_sources(chunks)
            yield f'data: {json.dumps({"type": "sources", "sources": sources})}\n\n'
            yield 'data: {"type": "done"}\n\n'

        except Exception as exc:
            logger.exception('stream_ask failed: %s', exc)
            yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'
            yield 'data: {"type": "done"}\n\n'

    # ── LangGraph ask ──────────────────────────────────────────────────────────

    def langgraph_ask(
        self,
        patient,
        query:     str,
        history:   List[Dict] = None,
        session_id: str       = None,
    ) -> Tuple[str, List[Dict]]:
        """
        Full LangGraph pipeline: router → scoped retrieval → generate → verify/retry.
        Returns (response_text, sources).
        """
        from rag_assistant.graph.graph import health_graph
        from rag_assistant.services.generation_service import _build_sources

        initial_state = {
            'question':      query,
            'route':         'general',
            'answer':        '',
            'patient_id':    patient.pk,
            'context_chunks': [],
            'session_id':    session_id,
            'history':       history or [],
            'needs_retry':   False,
            'retry_count':   0,
        }

        try:
            result  = health_graph.invoke(initial_state)
            answer  = result.get('answer', '')
            sources = _build_sources(result.get('context_chunks', []))
            logger.info(
                'LangGraph: route=%s retry=%d chunks=%d',
                result.get('route'), result.get('retry_count', 0),
                len(result.get('context_chunks', [])),
            )
            return answer or _no_answer_fallback(), sources
        except Exception as exc:
            logger.exception('langgraph_ask failed: %s', exc)
            return _no_answer_fallback(), []

    # ── Index status ───────────────────────────────────────────────────────────

    def index_status(self, patient) -> Dict[str, Any]:
        from rag_assistant.models import MedicalChunk, MedicalDocument
        from medical_records.models import MedicalRecord

        total_records   = MedicalRecord.objects.filter(patient=patient).count()
        total_docs      = MedicalDocument.objects.filter(patient=patient).count()
        total_chunks    = MedicalChunk.objects.filter(patient=patient).count()
        embedded_chunks = MedicalChunk.objects.filter(patient=patient, embedding__isnull=False).count()

        return {
            'medical_records':  total_records,
            'rag_documents':    total_docs,
            'total_chunks':     total_chunks,
            'embedded_chunks':  embedded_chunks,
            'coverage_pct':     round(embedded_chunks / max(total_chunks, 1) * 100, 1),
        }


def _no_answer_fallback() -> str:
    return (
        "I was unable to find relevant information in your health records for this question. "
        "Please try rephrasing, or consult your healthcare provider directly.\n\n"
        "*Always consult your doctor for medical advice.*"
    )

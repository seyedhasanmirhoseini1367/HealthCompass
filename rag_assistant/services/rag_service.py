# rag_assistant/services/rag_service.py
"""
RAG orchestrator — ties together document processing, embedding, retrieval
and generation into a single public interface.

Supports three query modes:
  ask()            — non-streaming (returns full response + sources)
  stream_ask()     — streaming (yields SSE-ready tokens)
  langgraph_ask()  — LangGraph pipeline with routing + self-correction

PhD proposal additions (all opt-in via RAG_CONFIG flags):
  • cold-start fallback  — when patient has no indexed records, return
    a helpful response with population reference ranges instead of an
    empty "no data" message  (COLD_START_ENABLED)
  • guardrail            — post-generation safety filter that appends
    disclaimers when dosage recommendations, definitive diagnoses, or
    emergency language is detected
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
    ) -> Tuple[str, List[Dict], str, int, bool, List]:
        """
        Returns (response_text, sources, llm_provider, retrieved_chunks_count,
                 safety_routed, triggered_rules).
        """
        from django.conf import settings
        from rag_assistant.services.generation_service import generate
        from rag_assistant.services.guardrail_service  import GuardrailService

        # ── Pre-query safety gate (before anything else) ──────────────────────
        is_emergency, emergency_response = GuardrailService.check_pre_query(query)
        if is_emergency:
            return emergency_response, [], 'safety_gate', 0, True, []

        # ── Cold-start check ──────────────────────────────────────────────────
        if settings.RAG_CONFIG.get('COLD_START_ENABLED', True):
            cold = self._cold_start_response(patient, query)
            if cold:
                return cold, [], 'cold_start', 0, False, []

        chunks = self.ret_svc.retrieve(
            patient       = patient,
            query         = query,
            document_type = document_type,
            top_k         = top_k,
        )
        response, sources, provider = generate(chunks, query, history or [])

        # ── Guardrail ─────────────────────────────────────────────────────────
        response, rules_fired = GuardrailService().apply(response)
        if rules_fired:
            logger.info('ask() guardrail rules fired: %s', rules_fired)

        return response, sources, provider, len(chunks), False, rules_fired

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
            data: {"type": "meta",    "provider": "gemini", "chunks": 6, "mode": "trajectory"|"standard"}
            data: {"type": "done"}
            data: {"type": "error",   "message": "..."}

        When the query is temporal (trend/trajectory keywords detected),
        TrajectoryService builds a chronologically ordered context and passes
        it to the LLM with the trajectory system prompt — bypassing similarity
        ranking so the LLM can reason about direction and slope.
        """
        import json
        from django.conf import settings
        from rag_assistant.services.generation_service import (
            generate_streaming, _build_sources, _build_general_sources,
            active_stream_provider,
        )
        from rag_assistant.services.guardrail_service        import GuardrailService
        from rag_assistant.services.trajectory_service       import TrajectoryService
        from rag_assistant.services.general_knowledge_service import (
            GeneralKnowledgeService, classify_query_mode,
        )

        try:
            # ── Pre-query safety gate ─────────────────────────────────────────
            is_emergency, emergency_response = GuardrailService.check_pre_query(query)
            if is_emergency:
                yield f'data: {json.dumps({"type": "token", "content": emergency_response})}\n\n'
                yield 'data: {"type": "sources", "sources": []}\n\n'
                yield f'data: {json.dumps({"type": "meta", "provider": "safety_gate", "chunks": 0, "mode": "emergency", "safety_routed": True, "triggered_rules": []})}\n\n'
                yield 'data: {"type": "done"}\n\n'
                return

            # ── Mode classification (Router Agent) ────────────────────────────
            query_mode = classify_query_mode(query)
            logger.info('stream_ask: query_mode=%s query=%r', query_mode, query[:60])

            general_chunks: List[Dict] = []
            chunks:         List[Dict] = []
            trajectory_context         = ''
            mode                       = query_mode

            gk_svc = GeneralKnowledgeService()

            if query_mode == 'general':
                # ── General knowledge only — no patient data touched ──────────
                general_chunks = gk_svc.retrieve(query)

            elif query_mode == 'hybrid':
                # ── Both sources ──────────────────────────────────────────────
                general_chunks = gk_svc.retrieve(query)
                intent = self.ret_svc.classify_query_intent(query)
                chunks = self.ret_svc.retrieve(
                    patient=patient, query=query,
                    document_type=document_type, query_intent=intent,
                )

            else:
                # ── Personal mode — original pipeline ─────────────────────────
                if settings.RAG_CONFIG.get('COLD_START_ENABLED', True):
                    cold = self._cold_start_response(patient, query)
                    if cold:
                        yield f'data: {json.dumps({"type": "token", "content": cold})}\n\n'
                        yield 'data: {"type": "sources", "sources": []}\n\n'
                        yield f'data: {json.dumps({"type": "meta", "provider": "cold_start", "chunks": 0, "mode": "cold_start"})}\n\n'
                        yield 'data: {"type": "done"}\n\n'
                        return

                traj_svc = TrajectoryService()
                if (
                    settings.RAG_CONFIG.get('TRAJECTORY_ENABLED', True)
                    and traj_svc.is_temporal_query(query)
                ):
                    trajectory_context, chunks = traj_svc.get_trajectory_context(patient, query)
                    if trajectory_context:
                        mode = 'trajectory'
                    else:
                        chunks = self.ret_svc.retrieve(
                            patient=patient, query=query,
                            document_type=document_type, query_intent='temporal',
                        )
                else:
                    intent = self.ret_svc.classify_query_intent(query)
                    chunks = self.ret_svc.retrieve(
                        patient=patient, query=query,
                        document_type=document_type, query_intent=intent,
                    )

                if not chunks and not trajectory_context and history:
                    trajectory_context = (
                        "No additional records were retrieved for this follow-up question. "
                        "Please answer based on the conversation history above."
                    )
                    mode = 'history_followup'

            # ── Streaming generation ──────────────────────────────────────────
            collected_tokens: List[str] = []
            for token in generate_streaming(
                chunks,
                query,
                history or [],
                context_override=trajectory_context,
                query_mode=query_mode,
                general_chunks=general_chunks,
            ):
                collected_tokens.append(token)
                yield f'data: {json.dumps({"type": "token", "content": token})}\n\n'

            # ── Guardrail ─────────────────────────────────────────────────────
            full_response = ''.join(collected_tokens)
            safe_response, rules_fired = GuardrailService().apply(full_response)
            if len(safe_response) > len(full_response):
                extra = safe_response[len(full_response):]
                yield f'data: {json.dumps({"type": "token", "content": extra})}\n\n'

            # ── Sources: merge personal + general ─────────────────────────────
            sources  = _build_sources(chunks)
            if general_chunks:
                sources += _build_general_sources(general_chunks)
            provider = active_stream_provider()
            yield f'data: {json.dumps({"type": "sources", "sources": sources})}\n\n'
            yield f'data: {json.dumps({"type": "meta", "provider": provider, "chunks": len(chunks) + len(general_chunks), "mode": mode, "safety_routed": False, "triggered_rules": rules_fired})}\n\n'

            # ── Inline chart for trajectory answers ───────────────────────────
            if mode == 'trajectory':
                try:
                    chart_data = traj_svc.get_chart_data(patient, query)
                    if chart_data:
                        yield f'data: {json.dumps({"type": "chart", "chart": chart_data})}\n\n'
                except Exception as _chart_err:
                    logger.warning('chart_data generation failed: %s', _chart_err)

            yield 'data: {"type": "done"}\n\n'

        except Exception as exc:
            logger.exception('stream_ask failed: %s', exc)
            yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'
            yield 'data: {"type": "done"}\n\n'

    # ── LangGraph ask ──────────────────────────────────────────────────────────

    def langgraph_ask(
        self,
        patient,
        query:      str,
        history:    List[Dict] = None,
        session_id: str        = None,
    ) -> Tuple[str, List[Dict], str, int]:
        """
        Full LangGraph pipeline: router → scoped retrieval → generate → verify/retry.
        Returns (response_text, sources, provider, chunks_count).
        """
        from django.conf import settings
        from rag_assistant.graph.graph import health_graph
        from rag_assistant.services.generation_service import _build_sources
        from rag_assistant.services.guardrail_service  import GuardrailService

        # ── Pre-query safety gate (before anything else) ──────────────────────
        is_emergency, emergency_response = GuardrailService.check_pre_query(query)
        if is_emergency:
            return emergency_response, [], 'safety_gate', 0

        # ── Cold-start check ──────────────────────────────────────────────────
        if settings.RAG_CONFIG.get('COLD_START_ENABLED', True):
            cold = self._cold_start_response(patient, query)
            if cold:
                return cold, [], 'cold_start', 0

        initial_state = {
            'question':          query,
            'route':             'general',
            'answer':            '',
            'patient_id':        patient.pk,
            'context_chunks':    [],
            'session_id':        session_id,
            'history':           history or [],
            'needs_retry':       False,
            'retry_count':       0,
            'llm_provider':      '',
            'trajectory_context': '',
        }

        try:
            result   = health_graph.invoke(initial_state)
            answer   = result.get('answer', '')
            chunks   = result.get('context_chunks', [])
            sources  = _build_sources(chunks)
            provider = result.get('llm_provider', '')

            logger.info(
                'LangGraph: route=%s provider=%s retry=%d chunks=%d trajectory=%s',
                result.get('route'), provider,
                result.get('retry_count', 0), len(chunks),
                bool(result.get('trajectory_context')),
            )

            final_answer = answer or _no_answer_fallback()

            # Guardrail on LangGraph output
            final_answer, rules_fired = GuardrailService().apply(final_answer)
            if rules_fired:
                logger.info('langgraph_ask() guardrail rules fired: %s', rules_fired)

            return final_answer, sources, provider, len(chunks)
        except Exception as exc:
            logger.exception('langgraph_ask failed: %s', exc)
            return _no_answer_fallback(), [], '', 0

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

    # ── Cold-start helper ──────────────────────────────────────────────────────

    def _cold_start_response(self, patient, query: str) -> Optional[str]:
        """
        Return a helpful response when the patient has no indexed chunks, or
        None if the patient does have records (normal path should proceed).

        The cold-start response:
        1. Acknowledges no records are indexed yet.
        2. Provides general population reference ranges for any biomarker
           mentioned in the query (so the response is still informative).
        3. Prompts the patient to upload their records.
        """
        from rag_assistant.models import MedicalChunk

        has_chunks = MedicalChunk.objects.filter(patient=patient).exists()
        if has_chunks:
            return None  # Normal path — records exist

        # Build biomarker reference info if a known biomarker was mentioned
        reference_info = _cold_start_reference(query)

        lines = [
            "I don't have any health records indexed for you yet, so I can't "
            "answer questions about your personal health data.\n",
        ]
        if reference_info:
            lines.append(reference_info)
        lines.append(
            "\n**To get personalised answers:** Upload your medical records "
            "(lab results, prescriptions, discharge summaries) using the "
            "Medical Records section. Once uploaded and indexed, I'll be able "
            "to answer questions specific to your health history.\n\n"
            "*Always consult your healthcare provider for medical advice.*"
        )
        return ''.join(lines)


# ── Cold-start reference ranges ───────────────────────────────────────────────

_COLD_START_REFERENCES = {
    'creatinine': (
        "**Creatinine — population reference ranges:**\n"
        "- Adult males: 0.7–1.3 mg/dL\n"
        "- Adult females: 0.6–1.1 mg/dL\n"
        "- Values above 1.3–1.4 mg/dL may warrant investigation of kidney function.\n"
    ),
    'hba1c': (
        "**HbA1c — population reference ranges:**\n"
        "- Normal: below 5.7%\n"
        "- Prediabetes: 5.7%–6.4%\n"
        "- Diabetes: 6.5% or above\n"
    ),
    'egfr': (
        "**eGFR (estimated Glomerular Filtration Rate) — population reference:**\n"
        "- Normal: ≥ 90 mL/min/1.73m²\n"
        "- Mild reduction: 60–89\n"
        "- Moderate reduction (CKD 3): 30–59\n"
        "- Severe reduction (CKD 4): 15–29\n"
    ),
    'glucose': (
        "**Fasting Blood Glucose — population reference ranges:**\n"
        "- Normal: 70–99 mg/dL (3.9–5.5 mmol/L)\n"
        "- Impaired fasting glucose: 100–125 mg/dL\n"
        "- Diabetes: ≥ 126 mg/dL on two occasions\n"
    ),
    'cholesterol': (
        "**Total Cholesterol — population reference:**\n"
        "- Desirable: below 200 mg/dL\n"
        "- Borderline high: 200–239 mg/dL\n"
        "- High: 240 mg/dL and above\n"
        "- LDL (desirable): below 100 mg/dL  |  HDL (protective): above 60 mg/dL\n"
    ),
}

# Biomarker alias → canonical key for cold-start lookup
_COLD_START_ALIASES = {
    'creatinine': 'creatinine',  'creat': 'creatinine',  'kidney': 'creatinine',
    'hba1c': 'hba1c',  'a1c': 'hba1c',  'hemoglobin a1c': 'hba1c',
    'egfr': 'egfr',  'gfr': 'egfr',
    'glucose': 'glucose',  'blood sugar': 'glucose',  'fasting glucose': 'glucose',
    'cholesterol': 'cholesterol',  'ldl': 'cholesterol',  'hdl': 'cholesterol',
}


def _cold_start_reference(query: str) -> str:
    q = query.lower()
    for alias, canonical in _COLD_START_ALIASES.items():
        if alias in q:
            return _COLD_START_REFERENCES.get(canonical, '')
    return ''


def _no_answer_fallback() -> str:
    return (
        "I was unable to find relevant information in your health records for this question. "
        "Please try rephrasing, or consult your healthcare provider directly.\n\n"
        "*Always consult your doctor for medical advice.*"
    )

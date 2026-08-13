# rag_assistant/services/rag_service.py
"""
RAG orchestrator — ties together document processing, embedding, retrieval
and generation into a single public interface.

Supports two query modes:
  ask()        — non-streaming (returns full response + sources)
  stream_ask() — streaming (yields SSE-ready tokens)

Both delegate to the single LangGraph pipeline in graph/graph.py.
stream_ask() uses the routing-only subgraph + generate_streaming() for
token-by-token SSE; ask() consumes that same SSE stream and assembles
the full response synchronously.

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
        from apps.rag_assistant.services.embedding_service  import EmbeddingService
        from apps.rag_assistant.services.retrieval_service  import RetrievalService
        self.emb_svc = EmbeddingService()
        self.ret_svc = RetrievalService()

    # ── Index ──────────────────────────────────────────────────────────────────

    def index_record(self, record) -> int:
        from apps.rag_assistant.services.document_processor import DocumentProcessor
        processor = DocumentProcessor()
        chunks    = processor.process_record(record)
        if chunks:
            self.emb_svc.embed_chunks(chunks)
        logger.info('Indexed record %s → %d chunks', record.pk, len(chunks))
        return len(chunks)

    def index_all_records(self, patient) -> int:
        from apps.medical_records.models import MedicalRecord
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
    ) -> Tuple[str, List[Dict], str, int, bool, List]:
        """
        Returns (response_text, sources, llm_provider, retrieved_chunks_count,
                 safety_routed, triggered_rules).

        Consumes stream_graph() SSE events synchronously and assembles the
        full response tuple — same shape callers expect.
        """
        import json
        from apps.accounts.consent import ConsentRequired, enforce_for_ai
        from apps.rag_assistant.graph.graph import stream_graph

        # Consent gate before anything reaches an external provider. Placed here
        # rather than at each view because ask() and stream_ask() are the only
        # two doors into the pipeline, for both the web UI and the mobile API.
        try:
            enforce_for_ai(patient)
        except ConsentRequired as exc:
            return exc.message, [], 'consent_required', 0, False, ['consent_required']

        tokens:          List[str]  = []
        sources:         List[Dict] = []
        provider:        str        = ''
        chunks_count:    int        = 0
        safety_routed:   bool       = False
        triggered_rules: List       = []

        for event_str in stream_graph(
            query         = query,
            patient       = patient,
            history       = history,
            document_type = document_type,
        ):
            if not event_str.startswith('data: '):
                continue
            try:
                payload = json.loads(event_str[6:].strip())
            except json.JSONDecodeError:
                continue
            t = payload.get('type')
            if t == 'token':
                tokens.append(payload.get('content', ''))
            elif t == 'sources':
                sources = payload.get('sources', [])
            elif t == 'meta':
                provider        = payload.get('provider', '')
                chunks_count    = payload.get('chunks', 0)
                safety_routed   = payload.get('safety_routed', False)
                triggered_rules = payload.get('triggered_rules', [])

        return ''.join(tokens), sources, provider, chunks_count, safety_routed, triggered_rules

    # ── Streaming ask ──────────────────────────────────────────────────────────

    def stream_ask(
        self,
        patient,
        query:         str,
        history:       List[Dict]    = None,
        document_type: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """
        Yields SSE-formatted events via the LangGraph pipeline.

            data: {"type": "token",   "content": "..."}
            data: {"type": "sources", "sources": [...]}
            data: {"type": "meta",    "provider": "groq", "chunks": 6, "mode": "trajectory"|"personal"|...}
            data: {"type": "chart",   "chart": {...}}    — trajectory queries only
            data: {"type": "done"}
            data: {"type": "error",   "message": "..."}
        """
        import json
        from apps.accounts.consent import ConsentRequired, enforce_for_ai
        from apps.rag_assistant.graph.graph import stream_graph

        try:
            enforce_for_ai(patient)
        except ConsentRequired as exc:
            # Emitted as ordinary stream events so existing clients render the
            # message in the chat transcript instead of failing the connection.
            yield f'data: {json.dumps({"type": "token", "content": exc.message})}\n\n'
            yield f'data: {json.dumps({"type": "meta", "provider": "consent_required", "chunks": 0, "safety_routed": False, "triggered_rules": ["consent_required"], "mode": "consent_required", "consent_purpose": exc.purpose})}\n\n'
            yield f'data: {json.dumps({"type": "done"})}\n\n'
            return

        yield from stream_graph(
            query         = query,
            patient       = patient,
            history       = history,
            document_type = document_type,
        )

    # ── Index status ───────────────────────────────────────────────────────────

    def index_status(self, patient) -> Dict[str, Any]:
        from apps.rag_assistant.models import MedicalChunk, MedicalDocument
        from apps.medical_records.models import MedicalRecord

        from apps.rag_assistant.services.embedding_service import audit_embeddings

        total_records   = MedicalRecord.objects.filter(patient=patient).count()
        total_docs      = MedicalDocument.objects.filter(patient=patient).count()
        total_chunks    = MedicalChunk.objects.filter(patient=patient).count()
        embedded_chunks = MedicalChunk.objects.filter(patient=patient, embedding__isnull=False).count()

        # Why coverage is short, not just that it is. A chunk with no vector is
        # excluded from retrieval, so without this an operator could see 60%
        # coverage and have no way to tell a failed embedding (retryable, and
        # the record is invisible to the assistant meanwhile) from a patient who
        # declined external processing (working as intended).
        Status = MedicalChunk.EmbeddingStatus
        unembedded = MedicalChunk.objects.filter(patient=patient, embedding__isnull=True)
        pending_chunks = unembedded.filter(embedding_status=Status.PENDING).count()
        failed_chunks  = unembedded.filter(embedding_status=Status.FAILED).count()
        blocked_chunks = unembedded.filter(embedding_status=Status.BLOCKED).count()

        # Embedded-but-incompatible chunks count toward coverage yet contribute
        # nothing to retrieval, so report them separately rather than letting a
        # healthy-looking coverage_pct hide a stale index.
        provenance = audit_embeddings(MedicalChunk, MedicalChunk.objects.filter(patient=patient))

        return {
            'medical_records':  total_records,
            'rag_documents':    total_docs,
            'total_chunks':     total_chunks,
            'embedded_chunks':  embedded_chunks,
            'coverage_pct':     round(embedded_chunks / max(total_chunks, 1) * 100, 1),
            'pending_chunks':   pending_chunks,
            'failed_chunks':    failed_chunks,
            'blocked_chunks':   blocked_chunks,
            'usable_chunks':    provenance['compatible'],
            'stale_chunks':     provenance['stale'],
            'embedding_model':  provenance['active_model'],
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
        from apps.rag_assistant.models import MedicalChunk

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

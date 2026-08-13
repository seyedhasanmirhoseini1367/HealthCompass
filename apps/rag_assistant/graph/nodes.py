# rag_assistant/graph/nodes.py
"""
LangGraph nodes for the HealthCompass RAG pipeline.

safety_gate_node — PRE-QUERY safety check (new, fires first).
                   Detects emergency symptoms and self-harm language.
                   If triggered, sets the final answer immediately and
                   routes to END — retrieval never fires.

router_node      — cold-start check FIRST (new), then keyword + semantic
                   temporal routing.  Semantic fallback uses cosine
                   similarity against prototype temporal phrases so that
                   queries like "explain my creatinine journey" still reach
                   trajectory_node even without explicit trend keywords.

trajectory_node  — PhD proposal Gap 1.
                   Calls TrajectoryService, which now implements the full
                   Δ(D,t,s) = sign(vₙ−v₁)·|vₙ−v₁|/σ_t·w(s) formula.

cold_start_node  — new LangGraph node (was service-layer only before).
                   Fires when the patient has zero indexed records.
                   Builds a population-reference context so the LLM can
                   still give an informative answer and prompt uploading.

*_node           — retrieval scoped to a medical domain.

Generation is deliberately NOT a node: stream_graph() calls generate_streaming()
directly so only generation tokens can reach the client.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from .state import HealthState

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SAFETY GATE — pre-query emergency / self-harm detection
# ══════════════════════════════════════════════════════════════════════════════

def safety_gate_node(state: HealthState) -> Dict[str, Any]:
    """
    First node in the graph.  Checks the raw query for emergency symptoms
    and self-harm language BEFORE any retrieval or LLM call.

    If triggered:
        • Sets 'answer' to a hard-coded emergency response (no LLM call).
        • Sets 'route' = 'emergency' so _route_from_safety_gate → END.

    If safe:
        • Returns the state unchanged; graph proceeds to router_node.
    """
    from apps.rag_assistant.services.guardrail_service import GuardrailService

    question = state.get('question', '')
    is_emergency, emergency_response = GuardrailService.check_pre_query(question)

    if is_emergency:
        logger.warning('safety_gate_node: emergency pattern — short-circuiting pipeline')
        return {
            'route':              'emergency',
            'answer':             emergency_response,
            'context_chunks':     [],
            'trajectory_context': '',
        }

    # Safe — proceed normally; echo route so LangGraph sees a non-empty update
    return {'route': state.get('route', 'general')}


# ══════════════════════════════════════════════════════════════════════════════
# UNDERSTAND — query understanding (QU) node
# ══════════════════════════════════════════════════════════════════════════════

def understand_node(state: HealthState) -> Dict[str, Any]:
    """
    Runs QueryUnderstanding to classify the query and rewrite follow-ups.

    Sets in state:
        route           — lab_results | medications | trajectory | … | general
        rewritten_query — standalone query with conversation context baked in
        mode            — personal | general | hybrid

    Falls back to _detect_route() (local keyword logic) if the QU service
    is unavailable (e.g. Groq API key missing, import error).
    """
    from apps.rag_assistant.services.query_understanding import understand

    question = state['question']
    history  = state.get('history', [])

    # The classifier sends the question to Groq, so it needs the patient for the
    # egress check. Resolved from the id already carried in the graph state.
    user = None
    try:
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.filter(pk=state.get('patient_id')).first()
    except Exception:
        pass

    try:
        qi = understand(question, history, user=user)
        # The question itself is health data — a patient asking about a
        # diagnosis reveals the diagnosis. Log the routing decision, never the
        # text, so ordinary application logs stay free of clinical content.
        logger.info(
            'understand_node: mode=%s route=%s intent=%s temporal=%s via_llm=%s q_len=%d',
            qi.mode, qi.route, qi.intent, qi.is_temporal, qi.via_llm, len(question),
        )
        return {
            'route':           qi.route,
            'rewritten_query': qi.rewritten_query,
            'mode':            qi.mode,
            'temporal_mode':   qi.temporal_mode,
        }
    except Exception as exc:
        logger.warning('understand_node failed (%s) — falling back to _detect_route', exc)
        return {
            'route':           _detect_route(question),
            'rewritten_query': question,
            'mode':            'personal',
            'temporal_mode':   None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER — cold-start check + keyword/semantic temporal routing
# ══════════════════════════════════════════════════════════════════════════════

_ROUTE_KEYWORDS: Dict[str, List[str]] = {
    'lab_results': [
        'lab', 'blood', 'test', 'result', 'cholesterol', 'glucose', 'hba1c',
        'haemoglobin', 'hemoglobin', 'creatinine', 'thyroid', 'tsh', 'wbc',
        'rbc', 'platelet', 'urine', 'bilirubin', 'ferritin', 'vitamin',
        'abnormal', 'reference range', 'measurement', 'level', 'sugar level',
        'iron', 'calcium', 'sodium', 'potassium', 'ldl', 'hdl', 'triglyceride',
        'egfr', 'gfr', 'kidney function',
    ],
    'medications': [
        'medication', 'medicine', 'drug', 'prescription', 'dose', 'dosage',
        'tablet', 'capsule', 'pill', 'mg', 'ml', 'side effect', 'interaction',
        'antibiotic', 'painkiller', 'insulin', 'statin', 'aspirin',
        'pharmacy', 'refill', 'treatment', 'taking', 'prescribed',
        'metformin', 'lisinopril', 'atorvastatin', 'omeprazole',
    ],
    'wearable': [
        'heart rate', 'steps', 'sleep', 'activity', 'calories', 'exercise',
        'workout', 'bpm', 'pulse', 'oxygen', 'spo2', 'fitbit', 'garmin',
        'oura', 'apple watch', 'wearable', 'sensor', 'tracker', 'distance',
        'resting', 'stress', 'hrv', 'heart rate variability', 'active minutes',
    ],
    'diagnosis': [
        'diagnosis', 'condition', 'disease', 'symptom', 'imaging', 'mri',
        'ct scan', 'x-ray', 'xray', 'ultrasound', 'ecg', 'eeg', 'scan',
        'report', 'radiology', 'discharge', 'hospital', 'surgery', 'procedure',
        'finding', 'impression', 'clinical note', 'assessment',
    ],
    'records': [
        'record', 'history', 'summary', 'timeline', 'overview', 'all',
        'previous', 'past', 'when', 'how long', 'last time', 'show me',
        'list', 'everything',
    ],
}

# Hard keyword list (fast path — no embedding required)
_TEMPORAL_ROUTE_KEYWORDS = [
    'getting better', 'getting worse', 'is it getting', 'is it improving',
    'is it worsening', 'has it improved', 'has it worsened',
    'over time', 'over the past', 'over the last', 'throughout the year',
    'compared to last', 'compared to before', 'compared to my previous',
    'since last', 'since my last', 'from last',
    'month by month', 'each visit', 'every visit', 'each time',
    'trend', 'trending', 'trajectory', 'progression', 'progress',
    'improving', 'worsening', 'deteriorating', 'declining', 'rising',
    'going up', 'going down', 'increasing', 'decreasing', 'elevated',
    'getting higher', 'getting lower', 'changed', 'change over',
    'how long have', 'how long has', 'timeline', 'history of my',
    'last 6 months', 'last 3 months', 'last year', 'last month',
    'fluctuating', 'stable over', 'consistently', 'pattern',
    'should i be worried', 'is this serious', 'is this concerning',
    'journey', 'paint me a picture', 'walk me through',
    'how has my', 'how have my', 'evolved', 'what happened to my',
]

def _kw_match(kw: str, q: str) -> bool:
    """Word-boundary keyword match — prevents 'mg' matching inside 'imaging'."""
    return bool(re.search(r'\b' + re.escape(kw) + r'\b', q))


def _is_temporal(question: str) -> bool:
    q = question.lower()
    return any(_kw_match(kw, q) for kw in _TEMPORAL_ROUTE_KEYWORDS)


def _detect_route(question: str) -> str:
    from django.conf import settings
    if settings.RAG_CONFIG.get('TRAJECTORY_ENABLED', True) and _is_temporal(question):
        logger.debug('RAG router: temporal query → trajectory')
        return 'trajectory'

    q       = question.lower()
    matched = [r for r, kws in _ROUTE_KEYWORDS.items() if any(_kw_match(kw, q) for kw in kws)]

    if len(matched) == 0:
        return 'general'
    if len(matched) == 1:
        return matched[0]
    logger.debug('Multi-route detected %s → records (all-types fallback)', matched)
    return 'records'


def router_node(state: HealthState) -> Dict[str, Any]:
    """
    Routing node — cold-start gate only.

    understand_node (which runs immediately before this) has already set
    state['route'] via QueryUnderstanding.  This node's only job is to
    check whether the patient has any indexed records; if not, it overrides
    the route to 'cold_start' so cold_start_node can handle the response.
    """
    from django.conf import settings

    if settings.RAG_CONFIG.get('COLD_START_ENABLED', True):
        try:
            from django.contrib.auth import get_user_model
            from apps.rag_assistant.models import MedicalChunk
            User    = get_user_model()
            patient = User.objects.get(pk=state['patient_id'])
            if not MedicalChunk.objects.filter(patient=patient).exists():
                logger.info('router_node: cold-start — no records indexed for patient %s', state['patient_id'])
                return {'route': 'cold_start', 'trajectory_context': ''}
        except Exception as exc:
            logger.warning('router_node cold-start check failed (proceeding normally): %s', exc)

    # Route already set by understand_node — echo it so LangGraph sees a non-empty update.
    logger.debug('router_node: route=%s q_len=%d', state.get('route'), len(state['question']))
    return {'route': state.get('route', 'general')}


# ══════════════════════════════════════════════════════════════════════════════
# COLD-START NODE
# ══════════════════════════════════════════════════════════════════════════════

def cold_start_node(state: HealthState) -> Dict[str, Any]:
    """
    Fires when the patient has no indexed records.

    Builds a structured context string containing population reference ranges
    for any biomarker mentioned in the query.  This is passed to generate_node
    as a context_override so the LLM can give an informative answer while
    clearly noting it has no personal data to draw from.
    """
    from apps.rag_assistant.services.rag_service import _cold_start_reference

    query          = state['question']
    reference_info = _cold_start_reference(query)

    if not reference_info:
        reference_info = (
            "No specific population reference ranges are available for this query. "
            "Please consult your healthcare provider or published clinical guidelines."
        )

    cold_context = (
        "IMPORTANT CONTEXT: This patient has not yet uploaded any medical records. "
        "You have no access to their personal health data. "
        "Do NOT make any claims about this specific patient's results or history. "
        "Answer the question using only general medical knowledge and the population "
        "reference data below. At the end of your response, encourage the patient "
        "to upload their medical records so you can give personalised answers.\n\n"
        f"POPULATION REFERENCE DATA:\n{reference_info}"
    )

    logger.info('cold_start_node: building population-reference context for patient %s', state['patient_id'])
    return {
        'trajectory_context': cold_context,
        'context_chunks':     [],
    }


# ══════════════════════════════════════════════════════════════════════════════
# TRAJECTORY NODE
# ══════════════════════════════════════════════════════════════════════════════

def trajectory_node(state: HealthState) -> Dict[str, Any]:
    """
    Build a chronological biomarker trajectory using the full Δ(D,t,s) formula.
    Stores the formatted context in state['trajectory_context'] and the source
    chunks in state['context_chunks'] for the sources panel.
    """
    # Use the QU-rewritten query so follow-ups like "what about last year?"
    # are resolved to self-contained phrases before trajectory extraction.
    query = state.get('rewritten_query') or state['question']
    try:
        from django.contrib.auth import get_user_model
        from apps.rag_assistant.services.trajectory_service import TrajectoryService

        User    = get_user_model()
        patient = User.objects.get(pk=state['patient_id'])
        svc     = TrajectoryService()
        context, source_chunks = svc.get_trajectory_context(
            patient, query, temporal_mode=state.get('temporal_mode'))

        if context:
            logger.info(
                'trajectory_node: Δ trajectory built (%d chars, %d source chunks)',
                len(context), len(source_chunks),
            )
        else:
            logger.info('trajectory_node: no trajectory data found — verify_node will retry')

        return {
            'trajectory_context': context,
            'context_chunks':     source_chunks,
        }
    except Exception as exc:
        logger.exception('trajectory_node failed: %s', exc)
        return {'trajectory_context': '', 'context_chunks': []}


# ══════════════════════════════════════════════════════════════════════════════
# RETRIEVAL NODES
# ══════════════════════════════════════════════════════════════════════════════

def _retrieve(
    state:         HealthState,
    document_type                = None,   # str | tuple[str, ...] | None
    top_k:         Optional[int] = None,
    query_intent:  Optional[str] = None,
) -> Dict[str, Any]:
    # Use the QU-rewritten query (standalone, history-resolved) when available.
    query = state.get('rewritten_query') or state['question']
    try:
        from django.contrib.auth import get_user_model
        from apps.rag_assistant.services.retrieval_service import RetrievalService

        User    = get_user_model()
        patient = User.objects.get(pk=state['patient_id'])
        svc     = RetrievalService()
        chunks  = svc.retrieve(
            patient       = patient,
            query         = query,
            document_type = document_type,
            top_k         = top_k,
            query_intent  = query_intent,
        )
        return {'context_chunks': chunks, 'trajectory_context': ''}
    except Exception as exc:
        logger.exception('Retrieval failed: %s', exc)
        return {'context_chunks': [], 'trajectory_context': ''}


def lab_results_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, 'lab_result', query_intent='lab_results')

#: A medication's *status* is recorded in clinical notes far more often than in
#: the prescription that started it ("metformin discontinued due to GI
#: intolerance"). Restricting this route to 'medication' meant a discontinuation
#: could never become a candidate, and the assistant answered questions about
#: current medication from the prescription alone — confidently, and wrong.
#: Deliberately narrow: notes are admitted, lab results and wearables are not.
MEDICATION_STATUS_TYPES = ('medication', 'note')


def medications_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, MEDICATION_STATUS_TYPES, query_intent='medication')

def wearable_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, 'wearable', query_intent='general')

def diagnosis_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, None, query_intent='diagnostic')

def records_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, None, top_k=10, query_intent='general')

def general_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, None, query_intent='general')


# ══════════════════════════════════════════════════════════════════════════════
# GENERATION happens outside the graph
# ══════════════════════════════════════════════════════════════════════════════
#
# generate_node and verify_node used to live here, wired into a second graph
# (`health_graph`) that nothing ever invoked. Generation is done by
# stream_graph() calling generate_streaming() directly, so tokens can only come
# from generation and never from an internal node.
#
# verify_node's retry-on-empty-retrieval loop was never reachable either. Its
# removal changes no behaviour — it was already not running — but it does remove
# the impression that empty retrieval is retried somewhere. It is not.

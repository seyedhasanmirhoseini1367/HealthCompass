# rag_assistant/graph/nodes.py
"""
LangGraph nodes for the HealthCompass RAG pipeline.

router_node  — keyword-based routing (free, no LLM call)
*_node       — retrieval scoped to a medical record type
generate_node — LLM generation (Gemini → Anthropic → OpenAI)
verify_node   — answer quality check, triggers retry if needed
"""
import logging
from typing import Any, Dict

from .state import HealthState

logger = logging.getLogger(__name__)

# ── Route keyword maps ─────────────────────────────────────────────────────────

_ROUTE_KEYWORDS = {
    'lab_results': [
        'lab', 'blood', 'test', 'result', 'cholesterol', 'glucose', 'hba1c',
        'haemoglobin', 'hemoglobin', 'creatinine', 'thyroid', 'tsh', 'wbc',
        'rbc', 'platelet', 'urine', 'bilirubin', 'ferritin', 'vitamin',
        'abnormal', 'reference range', 'measurement', 'level',
    ],
    'medications': [
        'medication', 'medicine', 'drug', 'prescription', 'dose', 'dosage',
        'tablet', 'capsule', 'pill', 'mg', 'ml', 'side effect', 'interaction',
        'antibiotic', 'painkiller', 'insulin', 'statin', 'aspirin',
        'pharmacy', 'refill', 'treatment',
    ],
    'wearable': [
        'heart rate', 'steps', 'sleep', 'activity', 'calories', 'exercise',
        'workout', 'bpm', 'pulse', 'oxygen', 'spo2', 'fitbit', 'garmin',
        'oura', 'apple watch', 'wearable', 'sensor', 'tracker', 'distance',
        'resting', 'stress', 'hrv',
    ],
    'diagnosis': [
        'diagnosis', 'condition', 'disease', 'symptom', 'imaging', 'mri',
        'ct scan', 'x-ray', 'xray', 'ultrasound', 'ecg', 'eeg', 'scan',
        'report', 'radiology', 'discharge', 'hospital', 'surgery', 'procedure',
    ],
    'records': [
        'record', 'history', 'summary', 'timeline', 'overview', 'all',
        'previous', 'past', 'when', 'how long', 'last time',
    ],
}


def _detect_route(question: str) -> str:
    q = question.lower()
    for route, keywords in _ROUTE_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return route
    return 'general'


# ── Router ────────────────────────────────────────────────────────────────────

def router_node(state: HealthState) -> Dict[str, Any]:
    route = _detect_route(state['question'])
    logger.debug('RAG router: "%s" → %s', state['question'][:60], route)
    return {'route': route}


# ── Retrieval helpers ─────────────────────────────────────────────────────────

def _retrieve(state: HealthState, document_type: str = None) -> Dict[str, Any]:
    try:
        from django.contrib.auth import get_user_model
        from rag_assistant.services.retrieval_service import RetrievalService

        User    = get_user_model()
        patient = User.objects.get(pk=state['patient_id'])
        svc     = RetrievalService()
        chunks  = svc.retrieve(
            patient       = patient,
            query         = state['question'],
            document_type = document_type,
        )
        return {'context_chunks': chunks}
    except Exception as exc:
        logger.exception('Retrieval failed: %s', exc)
        return {'context_chunks': []}


# ── Search nodes ───────────────────────────────────────────────────────────────

def lab_results_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, 'lab_result')

def medications_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, 'medication')

def wearable_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, 'wearable')

def diagnosis_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, 'note')

def records_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, None)   # all types

def general_node(state: HealthState) -> Dict[str, Any]:
    return _retrieve(state, None)   # all types


# ── Generate ──────────────────────────────────────────────────────────────────

def generate_node(state: HealthState) -> Dict[str, Any]:
    try:
        from rag_assistant.services.generation_service import generate
        answer, _ = generate(
            chunks  = state.get('context_chunks', []),
            query   = state['question'],
            history = state.get('history', []),
        )
        return {'answer': answer}
    except Exception as exc:
        logger.exception('Generation failed: %s', exc)
        return {'answer': 'I was unable to generate a response. Please try again.'}


# ── Verify ────────────────────────────────────────────────────────────────────

_UNCERTAIN_PHRASES = [
    "i don't know", "i'm not sure", "i cannot", "i can't",
    "no information", "not found", "unable to find",
]

def verify_node(state: HealthState) -> Dict[str, Any]:
    answer      = state.get('answer', '')
    retry_count = state.get('retry_count', 0)

    too_short   = len(answer.split()) < 15
    uncertain   = any(p in answer.lower() for p in _UNCERTAIN_PHRASES)
    no_chunks   = len(state.get('context_chunks', [])) == 0

    needs_retry = (too_short or uncertain or no_chunks) and retry_count < 2

    return {
        'needs_retry': needs_retry,
        'retry_count': retry_count + 1,
    }

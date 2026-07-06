# rag_assistant/graph/state.py
from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict


class HealthState(TypedDict):
    # Core
    question:       str
    route:          str   # "lab_results"|"medications"|"wearable"|"diagnosis"|"records"|"general"|"trajectory"
    answer:         str

    # Patient context
    patient_id:     Optional[int]

    # Retrieval
    context_chunks: List[Dict[str, Any]]

    # Session / history
    session_id:     Optional[str]
    history:        List[Dict[str, str]]

    # Self-correction
    needs_retry:    bool
    retry_count:    int

    # Generation metadata (populated by generate_node)
    llm_provider:   str   # 'gemini' | 'anthropic' | 'openai' | 'fallback' | 'error'

    # ── Trajectory reasoning (PhD proposal Gap 1) ─────────────────────────────
    # trajectory_context is a pre-formatted chronological context string built
    # by TrajectoryService.  When non-empty, generate_node uses it in place of
    # the standard similarity-ranked context so the LLM can reason about trends.
    trajectory_context: str   # '' when not a temporal query

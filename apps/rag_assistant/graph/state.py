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

    # ── Query Understanding (set by understand_node) ───────────────────────────
    # rewritten_query: standalone version of the question with conversation
    #   context baked in so follow-ups like "what about last year?" become
    #   self-contained before hitting the embedding model.
    # mode: 'personal' | 'general' | 'hybrid' — drives system-prompt selection
    #   in generate_node (Phase 2) and general-knowledge retrieval.
    # Both fields default to '' in initial state; nodes use .get() to read them.
    rewritten_query: str   # '' → nodes fall back to state['question']
    mode:            str   # '' | 'personal' | 'general' | 'hybrid'

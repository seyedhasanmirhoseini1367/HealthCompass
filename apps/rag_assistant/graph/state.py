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

    # Generation metadata
    llm_provider:   str   # 'gemini' | 'anthropic' | 'openai' | 'fallback' | 'error'

    # ── Trajectory reasoning (PhD proposal Gap 1) ─────────────────────────────
    # trajectory_context is a pre-formatted chronological context string built
    # by TrajectoryService.  When non-empty, generation uses it in place of
    # the standard similarity-ranked context so the LLM can reason about trends.
    trajectory_context: str   # '' when not a temporal query

    # Which temporal question was asked: 'latest' | 'previous' | 'trend' | None.
    # Distinguishes "what is my latest glucose" (one value) from
    # "is it getting worse" (the whole series) — both route to trajectory.
    temporal_mode:  Optional[str]

    # ── Query Understanding (set by understand_node) ───────────────────────────
    # rewritten_query: standalone version of the question with conversation
    #   context baked in so follow-ups like "what about last year?" become
    #   self-contained before hitting the embedding model.
    # mode: 'personal' | 'general' | 'hybrid' — drives system-prompt selection
    #   during generation and general-knowledge retrieval.
    # Both fields default to '' in initial state; nodes use .get() to read them.
    rewritten_query: str   # '' → nodes fall back to state['question']
    mode:            str   # '' | 'personal' | 'general' | 'hybrid'

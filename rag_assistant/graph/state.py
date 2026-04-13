# rag_assistant/graph/state.py
from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict


class HealthState(TypedDict):
    # Core
    question:       str
    route:          str   # "lab_results"|"medications"|"wearable"|"diagnosis"|"records"|"general"
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

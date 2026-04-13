# rag_assistant/rag_engine.py
"""
Backward-compatible shim.
All real logic lives in rag_assistant/services/.
"""
import logging
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)


def ask_gemini(user, query: str, history: list) -> Tuple[str, list]:
    """Entry point used by views.py — delegates to RAGService."""
    try:
        from rag_assistant.services.rag_service import RAGService
        svc = RAGService()
        return svc.ask(patient=user, query=query, history=history)
    except Exception as e:
        logger.exception('RAGService.ask failed: %s', e)
        return _fallback(), []


def ask_claude(user, query: str, history: list) -> Tuple[str, list]:
    """Alias kept for any code that calls ask_claude directly."""
    return ask_gemini(user, query, history)


def build_context(user, query: str, max_records: int = 8) -> str:
    """Legacy helper — kept so nothing breaks if called externally."""
    try:
        from rag_assistant.services.retrieval_service import RetrievalService
        svc    = RetrievalService()
        chunks = svc.retrieve(patient=user, query=query, top_k=max_records)
        if not chunks:
            return "No relevant records found."
        lines = ["=== PATIENT HEALTH RECORDS ===\n"]
        for c in chunks:
            m = c.get('metadata', {})
            lines.append(f"--- {m.get('document_title','Record')} ---")
            if m.get('record_date'):
                lines.append(f"Date: {m['record_date']}")
            lines.append(c['text'])
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        logger.exception('build_context failed: %s', e)
        return "Unable to retrieve records."


def _fallback() -> str:
    return (
        "I'm unable to connect to the AI service right now. "
        "Please ensure at least one API key (GEMINI_API_KEY, ANTHROPIC_API_KEY, or "
        "OPENAI_API_KEY) is configured in your .env file.\n\n"
        "*Note: Always consult your doctor for medical advice.*"
    )

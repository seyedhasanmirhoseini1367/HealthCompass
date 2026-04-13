# rag_assistant/services/generation_service.py
"""
LLM generation with provider chain: Gemini → Anthropic → OpenAI → fallback.

Each provider is tried in order; the first one that has a configured API key
and returns successfully is used.  This gives maximum resilience.
"""
import logging
from typing import List, Dict, Any, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are HealthCompass AI, a knowledgeable medical assistant that helps
patients understand their own health records, lab results, medications, and wearable data.

IMPORTANT RULES:
- You are NOT a doctor. Always remind the patient to consult their healthcare provider
  for medical decisions, diagnosis, or treatment changes.
- Never diagnose conditions. You may explain what a test or a value means.
- Be empathetic, clear, and avoid unnecessary medical jargon.
- If a value is abnormal, explain what it means but do NOT alarm the patient unnecessarily.
- Always reference the specific data from the patient's records when it is relevant.
- If you don't have enough information, say so honestly.
- Keep your answers concise and well-structured (use bullet points when helpful).

The patient's most relevant health records are provided below as context.
"""


def _build_context(chunks: List[Dict[str, Any]]) -> str:
    if not chunks:
        return "No relevant medical records were found for this question."
    parts = ["=== RELEVANT HEALTH RECORDS (ranked by relevance) ===\n"]
    for i, c in enumerate(chunks, 1):
        m     = c.get('metadata', {})
        title = m.get('document_title', 'Record')
        dtype = m.get('document_type', '')
        rdate = m.get('record_date', '')
        score = c.get('score', 0)
        header = f"[{i}] {title}"
        if dtype:
            header += f" ({dtype})"
        if rdate:
            header += f" — {rdate}"
        parts.append(header)
        parts.append(c['text'])
        parts.append("")
    return "\n".join(parts)


def _build_messages(context: str, query: str, history: List[Dict]) -> List[Dict]:
    messages = []
    for h in history[-6:]:
        messages.append({"role": "user",      "content": h.get('query', '')})
        messages.append({"role": "assistant", "content": h.get('response', '')})
    messages.append({"role": "user", "content": f"{context}\n\n=== PATIENT QUESTION ===\n{query}"})
    return messages


# ── Provider: Gemini ──────────────────────────────────────────────────────────

def _ask_gemini(context: str, query: str, history: List[Dict]) -> str:
    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError('No GEMINI_API_KEY')

    import google.generativeai as genai
    cfg = settings.RAG_CONFIG
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(cfg['GEMINI_MODEL'])

    gemini_history = []
    for h in history[-6:]:
        gemini_history.append({'role': 'user',  'parts': [h.get('query', '')]})
        gemini_history.append({'role': 'model', 'parts': [h.get('response', '')]})

    full_prompt = f"{SYSTEM_PROMPT}\n\n{context}\n\n=== PATIENT QUESTION ===\n{query}"

    if gemini_history:
        chat     = model.start_chat(history=gemini_history)
        response = chat.send_message(full_prompt)
    else:
        response = model.generate_content(full_prompt)

    return response.text


# ── Provider: Anthropic ───────────────────────────────────────────────────────

def _ask_anthropic(context: str, query: str, history: List[Dict]) -> str:
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        raise RuntimeError('No ANTHROPIC_API_KEY')

    import anthropic
    cfg    = settings.RAG_CONFIG
    client = anthropic.Anthropic(api_key=api_key)
    msgs   = _build_messages(context, query, history)

    resp = client.messages.create(
        model      = cfg['ANTHROPIC_MODEL'],
        max_tokens = cfg['MAX_TOKENS'],
        system     = SYSTEM_PROMPT,
        messages   = msgs,
    )
    return resp.content[0].text


# ── Provider: OpenAI ──────────────────────────────────────────────────────────

def _ask_openai(context: str, query: str, history: List[Dict]) -> str:
    api_key = getattr(settings, 'OPENAI_API_KEY', '')
    if not api_key:
        raise RuntimeError('No OPENAI_API_KEY')

    from openai import OpenAI
    cfg    = settings.RAG_CONFIG
    client = OpenAI(api_key=api_key)
    msgs   = [{"role": "system", "content": SYSTEM_PROMPT}] + _build_messages(context, query, history)

    resp = client.chat.completions.create(
        model      = cfg['OPENAI_MODEL'],
        messages   = msgs,
        max_tokens = cfg['MAX_TOKENS'],
    )
    return resp.choices[0].message.content


# ── Public generate function ──────────────────────────────────────────────────

def generate(
    chunks:  List[Dict[str, Any]],
    query:   str,
    history: List[Dict],
) -> Tuple[str, List[Dict]]:
    """
    Generate a response from retrieved chunks.
    Returns (response_text, sources_list).
    Tries Gemini → Anthropic → OpenAI in order.
    """
    context = _build_context(chunks)

    providers = [
        ('Gemini',    _ask_gemini),
        ('Anthropic', _ask_anthropic),
        ('OpenAI',    _ask_openai),
    ]

    for name, fn in providers:
        try:
            text = fn(context, query, history)
            logger.info('RAG generation via %s succeeded', name)
            sources = _build_sources(chunks)
            return text, sources
        except RuntimeError as e:
            # No API key — skip silently
            logger.debug('Skipping %s: %s', name, e)
        except Exception as e:
            logger.warning('%s generation failed: %s', name, e)

    # All providers failed
    return _fallback(), []


def _build_sources(chunks: List[Dict]) -> List[Dict]:
    seen    = set()
    sources = []
    for c in chunks:
        m   = c.get('metadata', {})
        did = m.get('document_id')
        if did and did not in seen:
            seen.add(did)
            sources.append({
                'title':         m.get('document_title', 'Record'),
                'document_type': m.get('document_type', ''),
                'record_date':   m.get('record_date', ''),
                'document_id':   did,
            })
    return sources


def _fallback() -> str:
    return (
        "I'm unable to connect to the AI service right now. "
        "Please check that at least one of GEMINI_API_KEY, ANTHROPIC_API_KEY, or "
        "OPENAI_API_KEY is configured in your .env file.\n\n"
        "*Note: Always consult your doctor for medical advice.*"
    )

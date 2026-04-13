# rag_assistant/services/generation_service.py
"""
LLM generation service — supports:
  • Gemini (google-genai v1.x)  — primary, free tier
  • Anthropic Claude            — fallback
  • OpenAI                      — fallback

Both regular (full response) and streaming (token-by-token SSE) modes.
"""
import logging
from typing import Any, Dict, Generator, List, Optional, Tuple

from django.conf import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are HealthCompass AI, a knowledgeable and empathetic medical assistant \
that helps patients understand their own health records, lab results, medications, and wearable data.

IMPORTANT RULES:
- You are NOT a doctor. Always remind the patient to consult their healthcare provider \
for medical decisions, diagnosis, or treatment changes.
- Never diagnose conditions. You may explain what a test or value means.
- Be empathetic, clear, and avoid unnecessary medical jargon.
- If a value is abnormal, explain what it means but do NOT alarm the patient unnecessarily.
- Always reference the specific data from the patient's records when relevant.
- If you don't have enough information, say so honestly.
- Keep answers concise and well-structured (use bullet points when helpful).
- End every response with a brief reminder to consult a healthcare professional.
"""


# ── Context builder ────────────────────────────────────────────────────────────

def _build_context(chunks: List[Dict[str, Any]]) -> str:
    if not chunks:
        return "No relevant medical records were found for this question."
    parts = ["=== RELEVANT HEALTH RECORDS (ranked by relevance) ===\n"]
    for i, c in enumerate(chunks, 1):
        m     = c.get('metadata', {})
        title = m.get('document_title', 'Record')
        dtype = m.get('document_type', '')
        rdate = m.get('record_date', '')
        header = f"[{i}] {title}"
        if dtype:  header += f" ({dtype})"
        if rdate:  header += f" — {rdate}"
        parts.append(header)
        parts.append(c.get('text', c.get('content', '')))
        parts.append("")
    return "\n".join(parts)


def _build_sources(chunks: List[Dict]) -> List[Dict]:
    seen, sources = set(), []
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


def _build_messages(context: str, query: str, history: List[Dict]) -> List[Dict]:
    messages = []
    for h in history[-6:]:
        messages.append({'role': 'user',      'content': h.get('query', '')})
        messages.append({'role': 'assistant', 'content': h.get('response', '')})
    messages.append({'role': 'user', 'content': f"{context}\n\n=== PATIENT QUESTION ===\n{query}"})
    return messages


# ── Non-streaming: Gemini ──────────────────────────────────────────────────────

def _call_gemini(context: str, query: str, history: List[Dict]) -> Optional[str]:
    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types

        client  = genai.Client(api_key=api_key)
        model   = settings.RAG_CONFIG['GEMINI_MODEL']
        messages = _build_messages(context, query, history)

        # Flatten history into Gemini contents list
        contents = []
        for m in messages[:-1]:
            contents.append(f"{'User' if m['role']=='user' else 'Assistant'}: {m['content']}")
        contents.append(messages[-1]['content'])

        response = client.models.generate_content(
            model    = model,
            contents = contents,
            config   = types.GenerateContentConfig(
                system_instruction = SYSTEM_PROMPT,
                max_output_tokens  = settings.RAG_CONFIG['MAX_TOKENS'],
                temperature        = 0.4,
            ),
        )
        return (response.text or '').strip() or None
    except Exception as exc:
        logger.warning('Gemini non-stream error: %s', exc)
        return None


# ── Non-streaming: Anthropic ───────────────────────────────────────────────────

def _call_anthropic(context: str, query: str, history: List[Dict]) -> Optional[str]:
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg    = client.messages.create(
            model      = settings.RAG_CONFIG['ANTHROPIC_MODEL'],
            max_tokens = settings.RAG_CONFIG['MAX_TOKENS'],
            system     = SYSTEM_PROMPT,
            messages   = _build_messages(context, query, history),
        )
        return msg.content[0].text
    except Exception as exc:
        logger.warning('Anthropic error: %s', exc)
        return None


# ── Non-streaming: OpenAI ──────────────────────────────────────────────────────

def _call_openai(context: str, query: str, history: List[Dict]) -> Optional[str]:
    api_key = getattr(settings, 'OPENAI_API_KEY', '')
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client   = OpenAI(api_key=api_key)
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}] + _build_messages(context, query, history)
        resp     = client.chat.completions.create(
            model      = settings.RAG_CONFIG['OPENAI_MODEL'],
            messages   = messages,
            max_tokens = settings.RAG_CONFIG['MAX_TOKENS'],
        )
        return resp.choices[0].message.content
    except Exception as exc:
        logger.warning('OpenAI error: %s', exc)
        return None


# ── Public: non-streaming ──────────────────────────────────────────────────────

def generate(
    chunks:  List[Dict[str, Any]],
    query:   str,
    history: List[Dict],
) -> Tuple[str, List[Dict]]:
    """Try Gemini → Anthropic → OpenAI. Returns (response_text, sources)."""
    context = _build_context(chunks)

    for caller in (_call_gemini, _call_anthropic, _call_openai):
        result = caller(context, query, history)
        if result:
            return result, _build_sources(chunks)

    return _fallback(), []


# ── Streaming: Gemini ──────────────────────────────────────────────────────────

def _stream_gemini(context: str, query: str, history: List[Dict]) -> Generator[str, None, None]:
    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        return
    try:
        from google import genai
        from google.genai import types

        client   = genai.Client(api_key=api_key)
        model    = settings.RAG_CONFIG['GEMINI_MODEL']
        messages = _build_messages(context, query, history)

        contents = []
        for m in messages[:-1]:
            contents.append(f"{'User' if m['role']=='user' else 'Assistant'}: {m['content']}")
        contents.append(messages[-1]['content'])

        for chunk in client.models.generate_content_stream(
            model    = model,
            contents = contents,
            config   = types.GenerateContentConfig(
                system_instruction = SYSTEM_PROMPT,
                max_output_tokens  = settings.RAG_CONFIG['MAX_TOKENS'],
                temperature        = 0.4,
            ),
        ):
            text = getattr(chunk, 'text', '') or ''
            if text:
                yield text

    except Exception as exc:
        logger.warning('Gemini stream error: %s', exc)
        yield from _stream_anthropic(context, query, history)


def _stream_anthropic(context: str, query: str, history: List[Dict]) -> Generator[str, None, None]:
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        yield from _stream_openai(context, query, history)
        return
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(
            model      = settings.RAG_CONFIG['ANTHROPIC_MODEL'],
            max_tokens = settings.RAG_CONFIG['MAX_TOKENS'],
            system     = SYSTEM_PROMPT,
            messages   = _build_messages(context, query, history),
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
    except Exception as exc:
        logger.warning('Anthropic stream error: %s', exc)
        yield from _stream_openai(context, query, history)


def _stream_openai(context: str, query: str, history: List[Dict]) -> Generator[str, None, None]:
    api_key = getattr(settings, 'OPENAI_API_KEY', '')
    if not api_key:
        return
    try:
        from openai import OpenAI
        client   = OpenAI(api_key=api_key)
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}] + _build_messages(context, query, history)
        stream   = client.chat.completions.create(
            model      = settings.RAG_CONFIG['OPENAI_MODEL'],
            messages   = messages,
            max_tokens = settings.RAG_CONFIG['MAX_TOKENS'],
            stream     = True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content or ''
            if text:
                yield text
    except Exception as exc:
        logger.warning('OpenAI stream error: %s', exc)


# ── Public: streaming ──────────────────────────────────────────────────────────

def generate_streaming(
    chunks:  List[Dict[str, Any]],
    query:   str,
    history: List[Dict],
) -> Generator[str, None, None]:
    """
    Yields text tokens one by one.
    Tracks whether anything was yielded — emits fallback if all LLMs fail.
    """
    context = _build_context(chunks)
    yielded = False

    def _track(gen):
        nonlocal yielded
        for token in gen:
            yielded = True
            yield token

    api_key_gemini    = getattr(settings, 'GEMINI_API_KEY', '')
    api_key_anthropic = getattr(settings, 'ANTHROPIC_API_KEY', '')
    api_key_openai    = getattr(settings, 'OPENAI_API_KEY', '')

    if api_key_gemini:
        yield from _track(_stream_gemini(context, query, history))
    elif api_key_anthropic:
        yield from _track(_stream_anthropic(context, query, history))
    elif api_key_openai:
        yield from _track(_stream_openai(context, query, history))

    if not yielded:
        yield _fallback()


# ── Fallback ───────────────────────────────────────────────────────────────────

def _fallback() -> str:
    return (
        "I'm unable to connect to the AI service right now. "
        "Please ensure GEMINI_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY "
        "is configured in your environment.\n\n"
        "*Always consult your doctor for medical advice.*"
    )

# rag_assistant/services/generation_service.py
"""
LLM generation service — supports:
  • Groq (llama-3.1-8b-instant) — primary, generous free tier
  • Gemini (google-genai v1.x)  — fallback
  • Anthropic Claude            — fallback
  • OpenAI                      — fallback

Both regular (full response) and streaming (token-by-token SSE) modes.
"""
import logging
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

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

GENERAL_KNOWLEDGE_SYSTEM_PROMPT = """You are HealthCompass AI, a knowledgeable health information assistant. \
You answer general health questions using only the provided excerpts from trusted Finnish \
clinical sources (Käypä hoito, Terveyskirjasto, THL).

IMPORTANT RULES:
- You are NOT a doctor. You provide health information, not personal medical advice.
- Answer ONLY from the provided source excerpts. Do not add information from your own training.
- Always cite the source name (e.g. "According to Käypä hoito...").
- Never diagnose the user. You may explain what conditions, tests, or values mean in general.
- If the excerpts do not cover the question, say so clearly and suggest consulting a doctor.
- Keep answers clear, structured, and free of unnecessary jargon.
- End with: "For personal medical advice, please consult your healthcare provider."
"""

HYBRID_SYSTEM_PROMPT = """You are HealthCompass AI. You have been given TWO sources of information:
1. Excerpts from trusted Finnish clinical guidelines (Käypä hoito, Terveyskirjasto, THL)
2. The patient's own health records

Use BOTH to answer the question:
- First explain the general concept using the clinical sources (cite them by name).
- Then personalise the answer using the patient's specific data from their records.
- Be empathetic and clear. Do not diagnose. Do not recommend specific medication changes.
- End with a reminder to discuss findings with their healthcare provider.

This combination — general knowledge + personal data — is the most valuable response you can give. \
Always make the personal data part explicit: "Looking at your own records..."
"""

# ── Trajectory-specific system prompt ─────────────────────────────────────────
# Used when context_override is supplied (i.e. trajectory mode is active).
# Instructs the LLM to reason about direction and magnitude, not just values.

TRAJECTORY_SYSTEM_PROMPT = """You are HealthCompass AI, a medical assistant specialising in \
interpreting longitudinal health data and trends.

You have been given a chronologically ordered set of health measurements. Your task is to \
reason about the TREND, not just the individual values.

TRAJECTORY REASONING RULES:
- Always comment on the DIRECTION of change (improving / worsening / stable).
- Quantify the change where possible (e.g. "increased by 133% over 11 months").
- Mention the RATE of change if the data allows (e.g. "rising approximately 0.11 mg/dL per month").
- Identify any inflection points (e.g. "values were stable until June, then accelerated").
- If a value has entered a clinically significant range (abnormal / critical), flag this clearly.
- Compare the patient's trajectory to typical clinical reference thresholds where relevant.
- Be empathetic but honest — do not downplay a clearly worsening trend.
- You are NOT a doctor. Strongly recommend consulting a healthcare provider, especially for \
  worsening or accelerating trends.
- End every response with a recommendation to discuss the trend with their doctor.
"""


# ── Context builder ────────────────────────────────────────────────────────────

def _build_general_context(chunks: List[Dict[str, Any]]) -> str:
    if not chunks:
        return "No relevant information found in the knowledge base for this question."
    parts = ["=== HEALTH INFORMATION FROM TRUSTED SOURCES ===\n"]
    for i, c in enumerate(chunks, 1):
        m      = c.get('metadata', {})
        title  = m.get('title', 'Article')
        source = m.get('source_name', '')
        url    = m.get('source_url', '')
        header = f"[{i}] {title}"
        if source: header += f" — {source}"
        if url:    header += f" ({url})"
        parts.append(header)
        parts.append(c.get('text', c.get('content', '')))
        parts.append("")
    return "\n".join(parts)


def _build_hybrid_context(
    personal_chunks: List[Dict[str, Any]],
    general_chunks:  List[Dict[str, Any]],
) -> str:
    parts = []
    if general_chunks:
        parts.append("=== CLINICAL GUIDELINES & HEALTH INFORMATION ===\n")
        for i, c in enumerate(general_chunks, 1):
            m      = c.get('metadata', {})
            title  = m.get('title', 'Article')
            source = m.get('source_name', '')
            parts.append(f"[G{i}] {title} — {source}")
            parts.append(c.get('text', c.get('content', '')))
            parts.append("")
    if personal_chunks:
        parts.append("=== PATIENT'S OWN HEALTH RECORDS ===\n")
        for i, c in enumerate(personal_chunks, 1):
            m     = c.get('metadata', {})
            title = m.get('document_title', 'Record')
            dtype = m.get('document_type', '')
            rdate = m.get('record_date', '')
            header = f"[P{i}] {title}"
            if dtype: header += f" ({dtype})"
            if rdate: header += f" — {rdate}"
            parts.append(header)
            parts.append(c.get('text', c.get('content', '')))
            parts.append("")
    return "\n".join(parts)


def _build_general_sources(chunks: List[Dict]) -> List[Dict]:
    seen, sources = set(), []
    for c in chunks:
        m   = c.get('metadata', {})
        cid = m.get('chunk_id')
        if cid and cid not in seen:
            seen.add(cid)
            sources.append({
                'title':       m.get('title', 'Article'),
                'source_name': m.get('source_name', ''),
                'source_url':  m.get('source_url', ''),
                'topic':       m.get('topic', ''),
                'is_general':  True,
            })
    return sources


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


def _resolve_context_and_prompt(
    chunks:           List[Dict[str, Any]],
    context_override: str = '',
    query_mode:       str = 'personal',
    general_chunks:   List[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Return (context_string, system_prompt_string).

    Modes:
      - trajectory context_override → TRAJECTORY_SYSTEM_PROMPT
      - general → general_chunks → GENERAL_KNOWLEDGE_SYSTEM_PROMPT
      - hybrid  → personal + general chunks → HYBRID_SYSTEM_PROMPT
      - personal (default) → personal chunks → SYSTEM_PROMPT
    """
    if context_override:
        return context_override, TRAJECTORY_SYSTEM_PROMPT
    if query_mode == 'general' and general_chunks:
        return _build_general_context(general_chunks), GENERAL_KNOWLEDGE_SYSTEM_PROMPT
    if query_mode == 'hybrid' and general_chunks:
        return _build_hybrid_context(chunks, general_chunks), HYBRID_SYSTEM_PROMPT
    return _build_context(chunks), SYSTEM_PROMPT


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
                'record_id':     m.get('record_id'),
            })
    return sources


def _build_messages(context: str, query: str, history: List[Dict]) -> List[Dict]:
    messages = []
    for h in history[-6:]:
        messages.append({'role': 'user',      'content': h.get('query', '')})
        messages.append({'role': 'assistant', 'content': h.get('response', '')})
    messages.append({'role': 'user', 'content': f"{context}\n\n=== PATIENT QUESTION ===\n{query}"})
    return messages


# ── Non-streaming: Groq ───────────────────────────────────────────────────────

def _call_groq(context: str, query: str, history: List[Dict], sys_prompt: str = SYSTEM_PROMPT) -> Optional[str]:
    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return None
    try:
        from groq import Groq
        client   = Groq(api_key=api_key)
        messages = [{'role': 'system', 'content': sys_prompt}] + _build_messages(context, query, history)
        resp     = client.chat.completions.create(
            model      = settings.RAG_CONFIG['GROQ_MODEL'],
            messages   = messages,
            max_tokens = settings.RAG_CONFIG['MAX_TOKENS'],
            temperature= 0.4,
        )
        return (resp.choices[0].message.content or '').strip() or None
    except Exception as exc:
        logger.warning('Groq non-stream error: %s', exc)
        return None


# ── Non-streaming: Gemini ──────────────────────────────────────────────────────

def _call_gemini(context: str, query: str, history: List[Dict], sys_prompt: str = SYSTEM_PROMPT) -> Optional[str]:
    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types

        client   = genai.Client(api_key=api_key)
        model    = settings.RAG_CONFIG['GEMINI_MODEL']
        messages = _build_messages(context, query, history)

        contents = []
        for m in messages[:-1]:
            role = 'user' if m['role'] == 'user' else 'model'
            contents.append({'role': role, 'parts': [{'text': m['content']}]})
        contents.append({'role': 'user', 'parts': [{'text': messages[-1]['content']}]})

        response = client.models.generate_content(
            model    = model,
            contents = contents,
            config   = types.GenerateContentConfig(
                system_instruction = sys_prompt,
                max_output_tokens  = settings.RAG_CONFIG['MAX_TOKENS'],
                temperature        = 0.4,
            ),
        )
        return (response.text or '').strip() or None
    except Exception as exc:
        logger.warning('Gemini non-stream error: %s', exc)
        return None


# ── Non-streaming: Anthropic ───────────────────────────────────────────────────

def _call_anthropic(context: str, query: str, history: List[Dict], sys_prompt: str = SYSTEM_PROMPT) -> Optional[str]:
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg    = client.messages.create(
            model      = settings.RAG_CONFIG['ANTHROPIC_MODEL'],
            max_tokens = settings.RAG_CONFIG['MAX_TOKENS'],
            system     = sys_prompt,
            messages   = _build_messages(context, query, history),
        )
        return msg.content[0].text
    except Exception as exc:
        logger.warning('Anthropic error: %s', exc)
        return None


# ── Non-streaming: OpenAI ──────────────────────────────────────────────────────

def _call_openai(context: str, query: str, history: List[Dict], sys_prompt: str = SYSTEM_PROMPT) -> Optional[str]:
    api_key = getattr(settings, 'OPENAI_API_KEY', '')
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client   = OpenAI(api_key=api_key)
        messages = [{'role': 'system', 'content': sys_prompt}] + _build_messages(context, query, history)
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
    chunks:           List[Dict[str, Any]],
    query:            str,
    history:          List[Dict],
    context_override: str = '',
    query_mode:       str = 'personal',
    general_chunks:   List[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict], str]:
    """
    Try Gemini → Anthropic → OpenAI.
    Returns (response_text, sources, provider_name).
    provider_name is one of: 'gemini', 'anthropic', 'openai', 'fallback'.

    When *context_override* is provided (trajectory mode), it is used as the
    context string and TRAJECTORY_SYSTEM_PROMPT is used instead of SYSTEM_PROMPT.
    """
    context, sys_prompt = _resolve_context_and_prompt(
        chunks, context_override, query_mode, general_chunks or []
    )

    for caller, name in [
        (_call_groq,      'groq'),
        (_call_gemini,    'gemini'),
        (_call_anthropic, 'anthropic'),
        (_call_openai,    'openai'),
    ]:
        result = caller(context, query, history, sys_prompt=sys_prompt)
        if result:
            all_sources = _build_sources(chunks)
            if query_mode in ('general', 'hybrid') and general_chunks:
                all_sources += _build_general_sources(general_chunks)
            return result, all_sources, name

    return _fallback(), [], 'fallback'


# ── Streaming: Groq ───────────────────────────────────────────────────────────

def _stream_groq(context: str, query: str, history: List[Dict], sys_prompt: str = SYSTEM_PROMPT) -> Generator[str, None, None]:
    api_key = getattr(settings, 'GROQ_API_KEY', '')
    if not api_key:
        return
    try:
        from groq import Groq
        client   = Groq(api_key=api_key)
        messages = [{'role': 'system', 'content': sys_prompt}] + _build_messages(context, query, history)
        stream   = client.chat.completions.create(
            model      = settings.RAG_CONFIG['GROQ_MODEL'],
            messages   = messages,
            max_tokens = settings.RAG_CONFIG['MAX_TOKENS'],
            temperature= 0.4,
            stream     = True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content or ''
            if text:
                yield text
    except Exception as exc:
        logger.warning('Groq stream error: %s', exc)
        yield from _stream_gemini(context, query, history, sys_prompt=sys_prompt)


# ── Streaming: Gemini ──────────────────────────────────────────────────────────

def _stream_gemini(context: str, query: str, history: List[Dict], sys_prompt: str = SYSTEM_PROMPT) -> Generator[str, None, None]:
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
            role = 'user' if m['role'] == 'user' else 'model'
            contents.append({'role': role, 'parts': [{'text': m['content']}]})
        contents.append({'role': 'user', 'parts': [{'text': messages[-1]['content']}]})

        for chunk in client.models.generate_content_stream(
            model    = model,
            contents = contents,
            config   = types.GenerateContentConfig(
                system_instruction = sys_prompt,
                max_output_tokens  = settings.RAG_CONFIG['MAX_TOKENS'],
                temperature        = 0.4,
            ),
        ):
            text = getattr(chunk, 'text', '') or ''
            if text:
                yield text

    except Exception as exc:
        logger.warning('Gemini stream error: %s', exc)
        yield from _stream_anthropic(context, query, history, sys_prompt=sys_prompt)


def _stream_anthropic(context: str, query: str, history: List[Dict], sys_prompt: str = SYSTEM_PROMPT) -> Generator[str, None, None]:
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        yield from _stream_openai(context, query, history, sys_prompt=sys_prompt)
        return
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(
            model      = settings.RAG_CONFIG['ANTHROPIC_MODEL'],
            max_tokens = settings.RAG_CONFIG['MAX_TOKENS'],
            system     = sys_prompt,
            messages   = _build_messages(context, query, history),
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
    except Exception as exc:
        logger.warning('Anthropic stream error: %s', exc)
        yield from _stream_openai(context, query, history, sys_prompt=sys_prompt)


def _stream_openai(context: str, query: str, history: List[Dict], sys_prompt: str = SYSTEM_PROMPT) -> Generator[str, None, None]:
    api_key = getattr(settings, 'OPENAI_API_KEY', '')
    if not api_key:
        return
    try:
        from openai import OpenAI
        client   = OpenAI(api_key=api_key)
        messages = [{'role': 'system', 'content': sys_prompt}] + _build_messages(context, query, history)
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
    chunks:           List[Dict[str, Any]],
    query:            str,
    history:          List[Dict],
    context_override: str = '',
    query_mode:       str = 'personal',
    general_chunks:   List[Dict[str, Any]] = None,
) -> Generator[str, None, None]:
    """
    Yields text tokens one by one.
    Respects context_override for trajectory mode.
    Tracks whether anything was yielded — emits fallback if all LLMs fail.
    """
    context, sys_prompt = _resolve_context_and_prompt(
        chunks, context_override, query_mode, general_chunks or []
    )
    yielded = False

    def _track(gen):
        nonlocal yielded
        for token in gen:
            yielded = True
            yield token

    api_key_groq      = getattr(settings, 'GROQ_API_KEY',      '')
    api_key_gemini    = getattr(settings, 'GEMINI_API_KEY',    '')
    api_key_anthropic = getattr(settings, 'ANTHROPIC_API_KEY', '')
    api_key_openai    = getattr(settings, 'OPENAI_API_KEY',    '')

    if api_key_groq:
        yield from _track(_stream_groq(context, query, history, sys_prompt=sys_prompt))
    elif api_key_gemini:
        yield from _track(_stream_gemini(context, query, history, sys_prompt=sys_prompt))
    elif api_key_anthropic:
        yield from _track(_stream_anthropic(context, query, history, sys_prompt=sys_prompt))
    elif api_key_openai:
        yield from _track(_stream_openai(context, query, history, sys_prompt=sys_prompt))

    if not yielded:
        yield _fallback()


# ── Provider detection (for logging streaming calls) ───────────────────────────

def active_stream_provider() -> str:
    """Return which provider would be used for a streaming call right now."""
    if getattr(settings, 'GROQ_API_KEY', ''):
        return 'groq'
    if getattr(settings, 'GEMINI_API_KEY', ''):
        return 'gemini'
    if getattr(settings, 'ANTHROPIC_API_KEY', ''):
        return 'anthropic'
    if getattr(settings, 'OPENAI_API_KEY', ''):
        return 'openai'
    return 'fallback'


# ── Fallback ───────────────────────────────────────────────────────────────────

def _fallback() -> str:
    return (
        "I'm unable to connect to the AI service right now. "
        "Please ensure GROQ_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY "
        "is configured in your environment.\n\n"
        "*Always consult your doctor for medical advice.*"
    )

# rag_assistant/services/query_understanding.py
"""
QueryUnderstanding — single entry point that replaces four separate classifiers:
  classify_query_mode()       → mode: personal | general | hybrid
  _detect_route()             → route: lab_results | medications | trajectory | …
  classify_query_intent()     → intent: temporal | medication | diagnostic | general
  TrajectoryService.is_temporal_query() → is_temporal: bool

Fast path: if a high-confidence keyword match exists, return instantly without
any LLM call (~0 ms). The LLM is called only for ambiguous or follow-up queries
where keyword matching fails.

Key improvement over the old system:
  rewritten_query — standalone version of the query with conversation context
  baked in. "What about last year?" with history ["creatinine is 1.4 mg/dL"]
  becomes "What was my creatinine level last year?" so retrieval works correctly
  even for short follow-ups.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── Shared keyword tables (single source of truth) ───────────────────────────
# Previously duplicated across nodes.py, general_knowledge_service.py,
# retrieval_service.py, and trajectory_service.py.

_PERSONAL_MARKERS = re.compile(
    r"\b(my|mine|i have|i am|am i|i've|i was|i've been|my last|my latest|"
    r"my recent|my results|my records|my levels|my history|my medications?|"
    r"my prescription|my diagnosis|for me|in my case)\b",
    re.IGNORECASE,
)

_GENERAL_PATTERNS = re.compile(
    r"\b(what is|what are|what does|what do|how does|how do|why is|why do|"
    r"explain|what causes|what happens|is it dangerous|is it serious|"
    r"what are the symptoms|how is it treated|what should i know about|"
    r"what is the difference|how common is|what medication|general(ly)?|"
    r"in general|normal range|reference range|typical|usually|normally)\b",
    re.IGNORECASE,
)

_TEMPORAL_KEYWORDS = {
    'getting better', 'getting worse', 'is it getting', 'is it improving',
    'is it worsening', 'has it improved', 'has it worsened',
    'over the past', 'over time', 'over the last', 'throughout the year',
    'compared to last', 'compared to before', 'compared to my previous',
    'since last', 'since my last', 'from last', 'month by month',
    'each visit', 'every visit', 'each time', 'trend', 'trending',
    'trajectory', 'progression', 'progress', 'improving', 'worsening',
    'deteriorating', 'declining', 'rising', 'going up', 'going down',
    'increasing', 'decreasing', 'elevated', 'getting higher', 'getting lower',
    'changed', 'change over', 'how long have', 'how long has',
    'timeline', 'history of my', 'last 6 months', 'last 3 months',
    'last year', 'last month', 'fluctuating', 'stable over',
    'consistently', 'pattern', 'journey', 'walk me through', 'how has my',
    'how have my', 'evolved', 'what happened to my',
}

_ROUTE_KEYWORDS = {
    'trajectory':  ['trend', 'over time', 'improving', 'getting worse', 'timeline',
                    'progression', 'trajectory', 'history of my', 'last year',
                    'last month', 'journey', 'how has my', 'how have my'],
    'lab_results': ['lab', 'blood test', 'cholesterol', 'glucose', 'hba1c', 'hgb',
                    'hemoglobin', 'haemoglobin', 'tsh', 'creatinine', 'egfr',
                    'vitamin', 'sodium', 'potassium', 'platelet', 'wbc', 'rbc',
                    'result', 'panel', 'test result', 'abnormal'],
    'medications': ['medication', 'medicine', 'drug', 'prescription', 'dose',
                    'dosage', 'tablet', 'capsule', 'pill', 'side effect',
                    'interaction', 'metformin', 'insulin', 'statin', 'taking',
                    'prescribed', 'pharmacy', 'refill', 'missed dose'],
    'wearable':    ['heart rate', 'steps', 'sleep', 'bpm', 'spo2', 'oxygen',
                    'fitbit', 'garmin', 'apple watch', 'oura', 'hrv', 'activity',
                    'exercise', 'calories', 'resting heart'],
    'diagnosis':   ['diagnosis', 'diagnosed', 'mri', 'ct scan', 'x-ray',
                    'ultrasound', 'imaging', 'discharge', 'surgery', 'procedure',
                    'clinical note', 'physician note', 'referral'],
    'records':     ['record', 'history', 'previous', 'last time', 'all my',
                    'upload', 'document'],
}

_INTENT_KEYWORDS = {
    'temporal':   list(_TEMPORAL_KEYWORDS),
    'medication': _ROUTE_KEYWORDS['medications'],
    'diagnostic': _ROUTE_KEYWORDS['diagnosis'],
    'general':    [],
}


@dataclass
class QueryIntent:
    mode:            str            # personal | general | hybrid
    route:           str            # lab_results | medications | trajectory | diagnosis | wearable | records | general
    intent:          str            # temporal | medication | diagnostic | general  (for α/β weights)
    is_temporal:     bool
    biomarker:       Optional[str]  # canonical biomarker name or None
    wants_chart:     bool
    rewritten_query: str            # standalone query with history context baked in
    via_llm:         bool = field(default=False)   # True when LLM path was taken


def understand(query: str, history: Optional[List[dict]] = None) -> QueryIntent:
    """
    Classify a user query. Returns a QueryIntent with all fields populated.

    Fast path (no LLM): high-confidence keyword match.
    Slow path (Groq):   ambiguous queries or short follow-ups that need
                        conversation context to make sense.
    """
    history = history or []

    # Short queries with no personal markers and no content keywords are almost
    # always follow-ups. Send to LLM so the rewritten_query is useful.
    is_followup = _is_followup(query, history)
    if is_followup:
        result = _llm_classify(query, history)
        if result:
            return result
        # LLM unavailable — fall through to keyword path with original query

    return _keyword_classify(query, history)


# ── Fast path ─────────────────────────────────────────────────────────────────

def _keyword_classify(query: str, history: List[dict]) -> QueryIntent:
    q_lower = query.lower()

    has_personal = bool(_PERSONAL_MARKERS.search(query))
    has_general  = bool(_GENERAL_PATTERNS.search(query))
    is_temporal  = _is_temporal_kw(q_lower)

    # Mode
    if not has_personal:
        mode = 'general'
    elif has_personal and has_general:
        mode = 'hybrid'
    else:
        mode = 'personal'

    # Route
    route = _route_kw(q_lower, is_temporal)

    # Intent (for retrieval α/β weights)
    intent = _intent_kw(q_lower)

    # Biomarker
    biomarker = _detect_biomarker(q_lower)

    # Chart
    wants_chart = any(kw in q_lower for kw in [
        'chart', 'graph', 'plot', 'diagram', 'visuali',
    ])

    return QueryIntent(
        mode=mode,
        route=route,
        intent=intent,
        is_temporal=is_temporal,
        biomarker=biomarker,
        wants_chart=wants_chart,
        rewritten_query=query,
        via_llm=False,
    )


def _is_temporal_kw(q_lower: str) -> bool:
    return any(_kw_match(kw, q_lower) for kw in _TEMPORAL_KEYWORDS)


def _route_kw(q_lower: str, is_temporal: bool) -> str:
    if is_temporal:
        return 'trajectory'
    matched = [r for r, kws in _ROUTE_KEYWORDS.items()
               if r != 'trajectory' and any(_kw_match(kw, q_lower) for kw in kws)]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        return 'records'
    return 'general'


def _intent_kw(q_lower: str) -> str:
    for intent, kws in _INTENT_KEYWORDS.items():
        if intent == 'general':
            continue
        if any(_kw_match(kw, q_lower) for kw in kws):
            return intent
    return 'general'


def _kw_match(kw: str, q: str) -> bool:
    return bool(re.search(r'\b' + re.escape(kw) + r'\b', q))


_BIOMARKER_ALIASES = {
    'creatinine': ['creatinine', 'serum creatinine', 'creat'],
    'hba1c':      ['hba1c', 'hemoglobin a1c', 'haemoglobin a1c', 'glycated hemoglobin', 'a1c'],
    'egfr':       ['egfr', 'gfr', 'glomerular filtration rate'],
    'glucose':    ['fasting glucose', 'blood glucose', 'blood sugar', 'glucose'],
    'cholesterol':['total cholesterol', 'ldl cholesterol', 'hdl cholesterol', 'ldl', 'hdl', 'cholesterol', 'triglyceride'],
    'hemoglobin': ['hemoglobin', 'haemoglobin', 'hgb'],
    'tsh':        ['thyroid stimulating hormone', 'tsh'],
    'vitamin_d':  ['25-hydroxyvitamin d', 'vitamin d', 'vit d'],
    'sodium':     ['sodium'],
    'potassium':  ['potassium'],
}


def _detect_biomarker(q_lower: str) -> Optional[str]:
    for canonical, aliases in _BIOMARKER_ALIASES.items():
        if any(alias in q_lower for alias in aliases):
            return canonical
    return None


def _is_followup(query: str, history: List[dict]) -> bool:
    """
    True when the query is short (<= 8 words) and has no personal markers or
    route keywords — most likely a pronoun-only follow-up like "what about
    last year?" or "is that bad?".
    """
    if not history:
        return False
    word_count = len(query.split())
    if word_count > 8:
        return False
    q_lower = query.lower()
    has_any_keyword = (
        bool(_PERSONAL_MARKERS.search(query))
        or any(_kw_match(kw, q_lower) for kws in _ROUTE_KEYWORDS.values() for kw in kws)
    )
    return not has_any_keyword


# ── Slow path (Groq) ──────────────────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = """You are a medical query classifier for a personal health assistant.
Given the user's current message and the recent conversation history, return a JSON object
with EXACTLY these fields:

{
  "mode":             "personal" | "general" | "hybrid",
  "route":            "trajectory" | "lab_results" | "medications" | "wearable" | "diagnosis" | "records" | "general",
  "intent":           "temporal" | "medication" | "diagnostic" | "general",
  "is_temporal":      true | false,
  "biomarker":        "<canonical name>" | null,
  "wants_chart":      true | false,
  "rewritten_query":  "<standalone version of the user's question, complete and self-contained>"
}

Rules:
- mode=personal when the user asks about their own data ("my", "mine", "I have")
- mode=general when the user asks about medical concepts without referencing their data
- mode=hybrid when both apply (e.g. "what is HbA1c and how does mine compare?")
- route=trajectory for trend/change-over-time questions; lab_results for specific test values;
  medications for drug/prescription questions; wearable for heart rate/steps/sleep;
  diagnosis for imaging/notes/discharge; records for general record history; general otherwise
- biomarker: one of creatinine, hba1c, egfr, glucose, cholesterol, hemoglobin, tsh, vitamin_d, sodium, potassium — or null
- rewritten_query: if the current query is a follow-up like "what about last year?",
  incorporate the biomarker/topic from conversation history to make it self-contained.
  If the query is already self-contained, copy it unchanged.
- Return ONLY valid JSON, no markdown fences, no explanation."""


def _llm_classify(query: str, history: List[dict]) -> Optional[QueryIntent]:
    try:
        from django.conf import settings
        api_key = getattr(settings, 'GROQ_API_KEY', '')
        if not api_key:
            return None

        from groq import Groq
        client = Groq(api_key=api_key)

        # Build a compact history summary (last 3 turns, 120 chars each)
        history_lines = []
        for msg in history[-3:]:
            role = msg.get('role', '')
            text = (msg.get('content') or '')[:120]
            if role in ('user', 'assistant'):
                history_lines.append(f"{role}: {text}")
        history_text = "\n".join(history_lines) or "(no prior conversation)"

        user_content = (
            f"Conversation history:\n{history_text}\n\n"
            f"Current user message: {query}"
        )

        resp = client.chat.completions.create(
            model='llama-3.1-8b-instant',
            messages=[
                {'role': 'system',  'content': _LLM_SYSTEM_PROMPT},
                {'role': 'user',    'content': user_content},
            ],
            temperature=0,
            max_tokens=256,
        )

        raw = resp.choices[0].message.content.strip()
        # Strip markdown fences if the model disobeys
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)

        return QueryIntent(
            mode=data.get('mode', 'personal'),
            route=data.get('route', 'general'),
            intent=data.get('intent', 'general'),
            is_temporal=bool(data.get('is_temporal', False)),
            biomarker=data.get('biomarker'),
            wants_chart=bool(data.get('wants_chart', False)),
            rewritten_query=data.get('rewritten_query', query) or query,
            via_llm=True,
        )

    except Exception as exc:
        logger.warning('QueryUnderstanding LLM path failed (%s), using keyword fallback', exc)
        return None

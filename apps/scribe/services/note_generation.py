"""
Transcript → SOAP note, using the project's existing LLM provider chain.

The model is asked for strict JSON in which every item cites the transcript
segments it came from. The output is validated here; one retry is allowed on a
validation failure, after which the encounter fails.
"""
import json
import logging
import re

from apps.rag_assistant.services import generation_service as gen

from .errors import ScribeError

logger = logging.getLogger(__name__)

SECTIONS = ('subjective', 'objective', 'assessment', 'plan')

SCRIBE_SYSTEM_PROMPT = """You are a clinical documentation assistant. You turn the \
transcript of a doctor–patient consultation into a SOAP note.

The transcript is given as numbered segments: "[index] [mm:ss-mm:ss] text". It has \
no speaker labels; infer from context who is the clinician and who is the patient.

STRICT RULES:
- Include ONLY information that is explicitly stated in the transcript.
- Never invent symptoms, findings, vital signs, test results, diagnoses, doses or plans.
- If nothing in the transcript supports a section, return an empty list for it.
- Subjective: what the patient reports (symptoms, history, concerns, medications they mention).
- Objective: examination findings, measurements and results that are stated aloud.
- Assessment: the clinician's stated impression or working diagnosis — not your own.
- Plan: tests, treatments, referrals, advice and follow-up that the clinician states.
- Each item is one short, factual statement in clinical style.
- Every item MUST list the indices of the transcript segments that support it.

OUTPUT FORMAT — reply with ONE JSON object and nothing else (no prose, no markdown):
{"subjective": [{"text": "...", "source_segments": [0, 1]}],
 "objective":  [],
 "assessment": [{"text": "...", "source_segments": [7]}],
 "plan":       [{"text": "...", "source_segments": [9, 10]}]}
""" + gen.UNTRUSTED_CONTENT_RULES

INSTRUCTION = ('Write the SOAP note for the consultation transcript above. '
               'Reply with only the JSON object described in the system prompt.')

_PROVIDERS = (
    ('groq',      gen._call_groq),
    ('gemini',    gen._call_gemini),
    ('anthropic', gen._call_anthropic),
    ('openai',    gen._call_openai),
)


class NoteValidationError(ValueError):
    pass


# ── Formatting ────────────────────────────────────────────────────────────────

def format_timestamp(seconds):
    seconds = int(seconds or 0)
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def format_transcript(segments):
    return '\n'.join(
        f'[{i}] [{format_timestamp(s["start"])}-{format_timestamp(s["end"])}] {s["text"]}'
        for i, s in enumerate(segments)
    )


# ── Validation ────────────────────────────────────────────────────────────────

_FENCE = re.compile(r'^```(?:json)?\s*|\s*```$', re.IGNORECASE)


def _extract_json_object(raw):
    text = _FENCE.sub('', (raw or '').strip())
    # The whole reply first, so a wrong top-level type (e.g. an array wrapping
    # the note) is reported rather than silently unwrapped by the slice below.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Otherwise tolerate prose around the object ("Here is the note: {...}").
    start, end = text.find('{'), text.rfind('}')
    if start == -1 or end <= start:
        raise NoteValidationError('no JSON object found in the reply')
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise NoteValidationError(f'invalid JSON ({exc.msg} at char {exc.pos})') from exc


def parse_note_json(raw, n_segments):
    """
    Validate an LLM reply and return the normalised SOAP dict.

    Structural problems (not JSON, missing section, wrong types, empty text)
    raise NoteValidationError. Segment indices that do not exist in the
    transcript are dropped rather than rejected: the item stays, and the UI
    shows it as having no cited source, so an unsupported statement is visible
    instead of silently trusted.
    """
    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        raise NoteValidationError('top-level value must be an object')

    note = {}
    for section in SECTIONS:
        if section not in data:
            raise NoteValidationError(f'missing section "{section}"')
        items = data[section]
        if not isinstance(items, list):
            raise NoteValidationError(f'section "{section}" must be a list')

        clean = []
        for pos, item in enumerate(items):
            where = f'{section}[{pos}]'
            if not isinstance(item, dict):
                raise NoteValidationError(f'{where} must be an object')
            text = item.get('text')
            if not isinstance(text, str) or not text.strip():
                raise NoteValidationError(f'{where}.text must be a non-empty string')
            sources = item.get('source_segments', [])
            if not isinstance(sources, list):
                raise NoteValidationError(f'{where}.source_segments must be a list')
            indices = sorted({
                s for s in sources
                if isinstance(s, int) and not isinstance(s, bool) and 0 <= s < n_segments
            })
            clean.append({'text': text.strip(), 'source_segments': indices})
        note[section] = clean
    return note


# ── LLM call ──────────────────────────────────────────────────────────────────

def call_llm(context, instruction):
    """
    First successful reply from the provider chain → (text, provider_name).

    Mirrors generation_service.generate(): providers without a key return None
    and are skipped, and providers not on the PHI allowlist are never tried.
    """
    from apps.accounts.egress import phi_provider_allowed

    for name, caller in _PROVIDERS:
        if not phi_provider_allowed(name):
            continue
        try:
            reply = caller(context, instruction, [], sys_prompt=SCRIBE_SYSTEM_PROMPT)
        except Exception as exc:
            logger.warning('scribe: provider %s raised %s', name, type(exc).__name__)
            continue
        if reply:
            return reply, name
    raise ScribeError('No language-model provider is configured or reachable.')


def generate_note_from_segments(segments):
    """Return (soap_dict, raw_output, provider). Raises ScribeError on failure."""
    if not segments:
        raise ScribeError('The transcript is empty.')

    context = format_transcript(segments)
    instruction = INSTRUCTION
    last_error = None
    raw = ''
    for attempt in (1, 2):
        raw, provider = call_llm(context, instruction)
        try:
            return parse_note_json(raw, len(segments)), raw, provider
        except NoteValidationError as exc:
            last_error = exc
            logger.warning('scribe: note JSON rejected (attempt %d): %s', attempt, exc)
            instruction = (f'{INSTRUCTION}\n\nYour previous reply was rejected: {exc}. '
                           'Reply with ONLY the JSON object, with all four sections.')
    raise ScribeError(f'The language model did not return a valid note: {last_error}.')


def generate_note(transcript):
    """Generate and store the Note for a Transcript."""
    from apps.accounts.consent import has_consent
    from apps.accounts.models import ConsentPurpose
    from apps.scribe.models import Note

    owner = transcript.encounter.owner
    if not has_consent(owner, ConsentPurpose.EXTERNAL_LLM):
        raise ScribeError('Generating the note sends the transcript to an external AI '
                          'provider, which needs your "external AI providers" consent.')

    soap, raw, provider = generate_note_from_segments(transcript.segments)
    note, _ = Note.objects.update_or_create(
        encounter=transcript.encounter,
        defaults={'soap': soap, 'raw_output': raw, 'provider': provider, 'evidence': {}},
    )
    return note

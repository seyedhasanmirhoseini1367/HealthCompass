"""Format-independent extraction: dates, regex lab values and LLM structuring."""
import re
import json
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)


_MONTH_NAMES = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
    'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'sept': 9,
    'oct': 10, 'nov': 11, 'dec': 12,
}


def _to_iso(y, m, d) -> str | None:
    try:
        return date(int(y), int(m), int(d)).strftime('%Y-%m-%d')
    except (ValueError, OverflowError):
        return None


def _parse_date_string(s: str, locale_hint: str | None = None) -> str | None:
    """
    Parse a standalone date string and return 'YYYY-MM-DD' or None.

    Three-layer disambiguation for numeric formats (d/m/Y vs m/d/Y):
      Layer 1 — Unambiguous formats first: ISO (YYYY-MM-DD) and alphabetic month
                 names. These carry no ambiguity regardless of locale.
      Layer 2 — Structural: if one of the two components is > 12, that component
                 must be the day (months only go to 12). '25/04' is always d/m.
      Layer 3 — Genuinely ambiguous (both ≤ 12, e.g. '03/04'): use locale_hint.
                 None → reads DATE_FORMAT_PREFERENCE from Django settings (default 'eu').

    locale_hint: 'eu' (day/month/year) | 'us' (month/day/year) | None (use setting)
    """
    if not s:
        return None
    s = s.strip().rstrip(',')

    # ── Layer 1a: ISO — YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD (unambiguous) ──
    iso_m = re.match(r'^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})$', s)
    if iso_m:
        return _to_iso(iso_m.group(1), iso_m.group(2), iso_m.group(3))

    # ── Layer 1b: Alphabetic month names (unambiguous) ────────────────────────
    for fmt in ('%B %d, %Y', '%B %d %Y', '%b %d, %Y', '%b %d %Y',
                '%d %B %Y', '%d %b %Y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue

    # ── Layers 2 & 3: Ambiguous numeric d/m/Y vs m/d/Y ───────────────────────
    num_m = re.match(r'^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})$', s)
    if not num_m:
        return None

    a, b = int(num_m.group(1)), int(num_m.group(2))
    y = num_m.group(3)
    if len(y) == 2:
        y = '20' + y
    y = int(y)

    # Layer 2: one component > 12 → it must be the day
    if a > 12 and b <= 12:
        return _to_iso(y, b, a)    # day=a, month=b
    if b > 12 and a <= 12:
        return _to_iso(y, a, b)    # day=b, month=a
    if a > 12 and b > 12:
        return None                 # impossible — both > 12

    # Layer 3: both ≤ 12, truly ambiguous — resolve via locale_hint
    hint = locale_hint
    if hint is None:
        from django.conf import settings as _s
        hint = getattr(_s, 'DATE_FORMAT_PREFERENCE', 'eu')

    if hint == 'eu':
        # d/m/Y: first component is day, second is month
        result = _to_iso(y, b, a)
        logger.debug('Ambiguous date %r → day=%d month=%d year=%d (locale=eu)', s, a, b, y)
    else:
        # m/d/Y: first component is month, second is day
        result = _to_iso(y, a, b)
        logger.debug('Ambiguous date %r → month=%d day=%d year=%d (locale=us)', s, a, b, y)
    return result


def _extract_date_regex(text: str, locale_hint: str | None = None) -> str | None:
    """
    Scan free-form text for a recognizable date and return 'YYYY-MM-DD' or None.

    locale_hint is forwarded to _parse_date_string for numeric dates that are
    ambiguous between d/m/Y (EU) and m/d/Y (US).
    None → reads DATE_FORMAT_PREFERENCE from Django settings.
    """
    # 1. Labelled date field: "Collection Date: 03/04/2024" / "Date: 2024-11-15"
    labelled = re.search(
        r'(?:collection\s+date|report\s+date|specimen\s+date|date\s+of\s+(?:service|birth|visit)|'
        r'test\s+date|sample\s+date|exam\s+date|date)\s*[:\-]\s*'
        r'((?:[A-Za-z]+ \d{1,2},? \d{4}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}))',
        text, re.IGNORECASE | re.MULTILINE,
    )
    if labelled:
        result = _parse_date_string(labelled.group(1).strip(), locale_hint=locale_hint)
        if result:
            return result

    # 2. Long-form "November 15, 2024" (unambiguous — month name removes ambiguity)
    m = re.search(
        r'\b(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b',
        text, re.IGNORECASE,
    )
    if m:
        month = _MONTH_NAMES.get(m.group(1).lower())
        if month:
            result = _to_iso(m.group(3), month, m.group(2))
            if result:
                return result

    # 3. Reversed "15 November 2024" (unambiguous)
    m = re.search(
        r'\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d{4})\b',
        text, re.IGNORECASE,
    )
    if m:
        month = _MONTH_NAMES.get(m.group(2).lower())
        if month:
            result = _to_iso(m.group(3), month, m.group(1))
            if result:
                return result

    # 4. ISO date YYYY-MM-DD (only 20xx years to avoid false positives)
    m = re.search(r'\b(20\d{2})[.\-/](0?[1-9]|1[0-2])[.\-/](0?[1-9]|[12]\d|3[01])\b', text)
    if m:
        result = _to_iso(m.group(1), m.group(2), m.group(3))
        if result:
            return result

    return None


def _extract_lab_values_regex(text: str) -> list:
    """
    Regex fallback: extract numeric lab values from free-form text.
    Handles formats like:
      Glucose: 5.2 mmol/L
      Hemoglobin  12.5  g/dL  (12.0 - 16.0)
      HbA1c    5.7    %    4.0-5.6
    """
    results = []
    seen = set()

    # Match: Name (colon or 2+ spaces) numeric_value optional_unit optional_ref
    pattern = re.compile(
        r'^[ \t]*'
        r'(?P<name>[A-Za-z][A-Za-z0-9 \-/()\+]+?)'       # test name
        r'(?:[ \t]*:[ \t]*|[ \t]{2,})'                    # separator
        r'(?P<value>\d{1,6}(?:\.\d{1,4})?)'               # numeric value
        r'(?:[ \t]+(?P<unit>[a-zA-Zµμ%][a-zA-Z0-9µμ%/·×^Ω\[\]\.]*))?' # unit (µ = U+00B5 MICRO SIGN, μ = U+03BC GREEK MU)
        r'(?:[ \t]+[\(\[]?[ \t]*(?P<ref>[\d][\d\.\- –]+\d)[ \t]*[\)\]]?)?',  # ref range
        re.MULTILINE,
    )

    for m in pattern.finditer(text):
        name = m.group('name').strip().rstrip(':- \t')
        value = m.group('value').strip()
        unit = (m.group('unit') or '').strip()
        ref = (m.group('ref') or '').strip()

        if len(name) < 2 or len(name) > 80:
            continue
        key = name.lower()
        if key in seen:
            continue
        try:
            fval = float(value)
            if fval < 0 or fval > 1_000_000:
                continue
        except ValueError:
            continue
        seen.add(key)
        results.append({'name': name, 'value': value, 'unit': unit,
                        'ref_range': ref, 'is_abnormal': False})

    return results


def _structure_medical_text_with_ai(text: str,
                                     table_lab_values: list | None = None,
                                     allow_external: bool = True) -> dict | None:
    """
    Call Gemini to extract structured medical data from free-form text.

    table_lab_values: pre-parsed lab rows from PDF table extraction.  When
    non-empty they take priority over AI-extracted values (structured cells are
    more accurate than AI parsing of flattened text).

    allow_external: when False the document text is never sent to Gemini and the
    local regex/table path is used instead. Callers pass the result of the
    consent check here — the decision is made where the user is known, and this
    function stays free of request context.

    Priority for lab_values: table > AI text > regex fallback.
    """
    from django.conf import settings
    api_key      = getattr(settings, 'GEMINI_API_KEY', '')
    gemini_model = settings.RAG_CONFIG.get('GEMINI_MODEL', 'gemini-2.5-flash')

    def _best_lab_values(ai_values=None):
        if table_lab_values:
            return table_lab_values
        return ai_values or _extract_lab_values_regex(text)

    # Same local path is taken when the key is absent and when external
    # processing is not permitted — no document text leaves the process either way.
    if not api_key or not allow_external:
        lab_values = _best_lab_values()
        return ({'record_type': 'lab_result', 'title': 'Lab Results',
                 'date': _extract_date_regex(text),
                 'lab_values': lab_values} if lab_values else None)

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(gemini_model)

        prompt = f"""Extract structured medical data from this text. Return JSON with:
{{
  "record_type": "lab_result|prescription|diagnosis|vaccination|imaging|discharge|other",
  "title": "short descriptive title (max 80 chars)",
  "date": "YYYY-MM-DD or null",
  "summary": "2-3 sentence summary",
  "lab_values": [{{"name":"","value":"","unit":"","ref_range":"","is_abnormal":false}}],
  "medications": [{{"name":"","dose":"","frequency":"","route":""}}],
  "diagnoses": [{{"code":"","description":""}}],
  "provider": "",
  "facility": ""
}}
Return only valid JSON, no markdown fences.

TEXT:
{text[:4000]}"""

        response = model.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)
        result['lab_values'] = _best_lab_values(result.get('lab_values'))
        if not result.get('date'):
            result['date'] = _extract_date_regex(text)
        return result
    except Exception as e:
        logger.warning('Gemini structuring failed: %s', e)
        lab_values = _best_lab_values()
        if lab_values:
            return {'record_type': 'lab_result', 'title': 'Lab Results',
                    'date': _extract_date_regex(text),
                    'lab_values': lab_values}
        return None

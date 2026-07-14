"""
Parsers for different medical record formats.
- KantaXMLParser: Finnish Kanta XML exports
- WearableCSVParser: CSV exports from Fitbit, Garmin, Oura, Apple Health
- PDFParser: PDF documents using pdfplumber + Gemini for structuring
"""
import io
import re
import csv
import json
import logging
from datetime import datetime, date
try:
    import defusedxml.ElementTree as ET
except ImportError:
    from xml.etree import ElementTree as ET  # fallback for local dev without defusedxml

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
                                     table_lab_values: list | None = None) -> dict | None:
    """
    Call Gemini to extract structured medical data from free-form text.

    table_lab_values: pre-parsed lab rows from PDF table extraction.  When
    non-empty they take priority over AI-extracted values (structured cells are
    more accurate than AI parsing of flattened text).

    Priority for lab_values: table > AI text > regex fallback.
    """
    from django.conf import settings
    api_key      = getattr(settings, 'GEMINI_API_KEY', '')
    gemini_model = settings.RAG_CONFIG.get('GEMINI_MODEL', 'gemini-2.5-flash')

    def _best_lab_values(ai_values=None):
        if table_lab_values:
            return table_lab_values
        return ai_values or _extract_lab_values_regex(text)

    if not api_key:
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


# ─── Kanta XML ──────────────────────────────────────────────────────────────

KANTA_NS = {
    'hl7': 'urn:hl7-org:v3',
    'ext': 'urn:hl7-org:sdtc',
}

# Namespaces used in CDA documents
_CDA_NS = 'urn:hl7-org:v3'


def _tag(local):
    return f'{{{_CDA_NS}}}{local}'


class KantaXMLParser:
    """Parse CDA-based Kanta XML exports into structured data."""

    def parse(self, xml_bytes: bytes) -> dict:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            return {'error': str(e), 'records': []}

        # Handle both single CDA document and bundle of documents
        # Kanta exports can be either a single ClinicalDocument or
        # a POCD wrapper containing multiple documents
        docs = root.findall(f'.//{_tag("ClinicalDocument")}')
        if not docs:
            # Root itself might be the ClinicalDocument
            if root.tag == _tag('ClinicalDocument'):
                docs = [root]
            else:
                docs = [root]

        records = []
        for doc in docs:
            parsed = self._parse_document(doc)
            if parsed:
                records.append(parsed)

        return {'records': records, 'count': len(records)}

    def _parse_document(self, doc) -> dict:
        result = {
            'type': self._get_doc_type(doc),
            'title': self._text(doc, f'.//{_tag("title")}'),
            'date': self._get_effective_time(doc),
            'author': self._get_author(doc),
            'organization': self._get_organization(doc),
            'sections': [],
        }

        for section in doc.findall(f'.//{_tag("section")}'):
            sec = self._parse_section(section)
            if sec:
                result['sections'].append(sec)

        return result

    def _parse_section(self, section) -> dict:
        code_el = section.find(_tag('code'))
        code = code_el.get('code', '') if code_el is not None else ''
        display = code_el.get('displayName', '') if code_el is not None else ''
        title = self._text(section, _tag('title'))
        text_el = section.find(_tag('text'))
        text_content = ''.join(text_el.itertext()).strip() if text_el is not None else ''

        entries = []
        for entry in section.findall(f'.//{_tag("observation")}'):
            obs = self._parse_observation(entry)
            if obs:
                entries.append(obs)

        for entry in section.findall(f'.//{_tag("substanceAdministration")}'):
            med = self._parse_medication(entry)
            if med:
                entries.append(med)

        return {
            'code': code,
            'display': display or title,
            'text': text_content[:2000],
            'entries': entries,
        }

    def _parse_observation(self, obs) -> dict | None:
        code_el = obs.find(_tag('code'))
        if code_el is None:
            return None
        name = code_el.get('displayName') or code_el.get('code', 'Unknown')

        value_el = obs.find(_tag('value'))
        value = unit = ref_low = ref_high = None
        is_abnormal = False

        if value_el is not None:
            value = value_el.get('value') or ''.join(value_el.itertext()).strip()
            unit = value_el.get('unit', '')

        # Reference range
        ref_el = obs.find(f'.//{_tag("referenceRange")}')
        if ref_el is not None:
            low = ref_el.find(f'.//{_tag("low")}')
            high = ref_el.find(f'.//{_tag("high")}')
            if low is not None:
                ref_low = low.get('value')
            if high is not None:
                ref_high = high.get('value')

        # Interpretation (H/L/N/A)
        interp_el = obs.find(_tag('interpretationCode'))
        if interp_el is not None:
            interp = interp_el.get('code', 'N')
            is_abnormal = interp in ('H', 'L', 'A', 'HH', 'LL')

        effective_time = self._get_effective_time(obs)

        return {
            'kind': 'lab',
            'name': name,
            'value': value,
            'unit': unit,
            'ref_low': ref_low,
            'ref_high': ref_high,
            'is_abnormal': is_abnormal,
            'date': effective_time,
        }

    def _parse_medication(self, subst) -> dict | None:
        consumable = subst.find(f'.//{_tag("name")}')
        name = ''.join(consumable.itertext()).strip() if consumable is not None else 'Unknown medication'

        dose_el = subst.find(_tag('doseQuantity'))
        dose = dose_el.get('value', '') if dose_el is not None else ''
        unit = dose_el.get('unit', '') if dose_el is not None else ''

        route_el = subst.find(_tag('routeCode'))
        route = route_el.get('displayName', '') if route_el is not None else ''

        return {
            'kind': 'medication',
            'name': name,
            'dose': dose,
            'unit': unit,
            'route': route,
            'date': self._get_effective_time(subst),
        }

    def _get_doc_type(self, doc) -> str:
        code_el = doc.find(_tag('code'))
        if code_el is not None:
            return code_el.get('displayName') or code_el.get('code', 'Document')
        return 'Document'

    def _get_effective_time(self, el) -> str | None:
        t = el.find(_tag('effectiveTime'))
        if t is None:
            t = el.find(f'.//{_tag("effectiveTime")}')
        if t is not None:
            val = t.get('value', '')
            return self._parse_hl7_date(val)
        return None

    def _get_author(self, doc) -> str:
        name_parts = []
        for author in doc.findall(f'.//{_tag("author")}'):
            given = self._text(author, f'.//{_tag("given")}')
            family = self._text(author, f'.//{_tag("family")}')
            if given or family:
                name_parts.append(f'{given} {family}'.strip())
        return ', '.join(name_parts) or 'Unknown'

    def _get_organization(self, doc) -> str:
        for org in doc.findall(f'.//{_tag("representedOrganization")}'):
            name = self._text(org, _tag('name'))
            if name:
                return name
        return ''

    def _text(self, el, path) -> str:
        found = el.find(path)
        if found is not None:
            return ''.join(found.itertext()).strip()
        return ''

    def _parse_hl7_date(self, val: str) -> str | None:
        if not val:
            return None
        val = val[:8]
        try:
            dt = datetime.strptime(val, '%Y%m%d')
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            return None  # malformed HL7 date — explicit None beats a raw string in callers


# ─── Wearable (CSV / JSON / Apple Health XML) ────────────────────────────────

# Column name mappings for common wearable exports
METRIC_COLUMN_MAP = {
    # Heart rate
    'heart rate': 'heart_rate',
    'heart_rate': 'heart_rate',
    'hr': 'heart_rate',
    'pulse': 'heart_rate',
    # Steps
    'steps': 'steps',
    'step count': 'steps',
    'total steps': 'steps',
    # Sleep
    'sleep': 'sleep',
    'sleep duration': 'sleep',
    'total sleep': 'sleep',
    'hours of sleep': 'sleep',
    # Calories
    'calories': 'calories',
    'active calories': 'calories',
    'calories burned': 'calories',
    'total calories': 'calories',
    # SpO2
    'spo2': 'blood_oxygen',
    'blood oxygen': 'blood_oxygen',
    'oxygen saturation': 'blood_oxygen',
    # Weight
    'weight': 'weight',
    'body weight': 'weight',
    # Temperature
    'temperature': 'temperature',
    'skin temperature': 'temperature',
    'body temperature': 'temperature',
}

METRIC_UNITS = {
    'heart_rate': 'bpm',
    'steps': 'steps',
    'sleep': 'hours',
    'calories': 'kcal',
    'blood_oxygen': '%',
    'weight': 'kg',
    'temperature': '°C',
    'other': '',
}


class WearableParser:
    """Parse wearable device exports: auto-detects CSV, JSON, or Apple Health XML."""

    # ── Public entry point ────────────────────────────────────────────────────

    def parse(self, data_bytes: bytes, filename: str = '') -> dict:
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        stripped = data_bytes.lstrip()
        if ext == 'parquet':
            return self._parse_parquet(data_bytes, filename)
        if ext == 'json' or stripped[:1] in (b'{', b'['):
            return self._parse_json(data_bytes, filename)
        if ext == 'xml' or stripped[:1] == b'<':
            return self._parse_xml(data_bytes, filename)
        return self._parse_csv(data_bytes, filename)

    # ── CSV ───────────────────────────────────────────────────────────────────

    def _parse_csv(self, data_bytes: bytes, filename: str = '') -> dict:
        try:
            text = data_bytes.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data_bytes.decode('latin-1')

        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        device = self._detect_device(filename, headers)
        data_points, errors = [], []

        for i, row in enumerate(reader):
            try:
                data_points.extend(self._parse_row(row, headers, device))
            except Exception as e:
                errors.append(f'Row {i+2}: {e}')

        return {'device': device, 'data_points': data_points, 'count': len(data_points), 'errors': errors[:10]}

    # ── Parquet ───────────────────────────────────────────────────────────────

    def _parse_parquet(self, data_bytes: bytes, filename: str = '') -> dict:
        try:
            import pandas as pd
        except ImportError:
            return {'device': 'unknown', 'data_points': [], 'count': 0,
                    'errors': ['pandas is required to read Parquet files.']}
        try:
            df = pd.read_parquet(io.BytesIO(data_bytes))
        except Exception as e:
            return {'device': 'unknown', 'data_points': [], 'count': 0, 'errors': [str(e)]}

        device = self._detect_device(filename, list(df.columns))
        data_points, errors = [], []

        # Normalise column names for matching
        df.columns = [str(c) for c in df.columns]

        # Find a datetime column
        dt_col = None
        for candidate in ('datetime', 'date', 'timestamp', 'time', 'startDate',
                          'start_time', 'creationDate'):
            if candidate in df.columns:
                dt_col = candidate
                break
        if dt_col is None:
            for col in df.columns:
                if 'date' in col.lower() or 'time' in col.lower():
                    dt_col = col
                    break

        for _, row in df.iterrows():
            # Parse datetime
            dt = None
            if dt_col:
                try:
                    import pandas as pd2
                    raw = row[dt_col]
                    if pd2.isna(raw):
                        continue
                    dt = pd.Timestamp(raw).to_pydatetime()
                except Exception:
                    pass
            if dt is None:
                continue

            for col, raw_val in row.items():
                if col == dt_col:
                    continue
                metric = self._map_metric(str(col))
                if metric is None:
                    continue
                try:
                    value = float(raw_val)
                    if pd.isna(value):
                        continue
                except (ValueError, TypeError):
                    continue
                value, unit = self._normalize_value(metric, value, col.lower())
                data_points.append({'metric': metric, 'value': value,
                                    'unit': unit, 'recorded_at': dt.isoformat()})

        return {'device': device, 'data_points': data_points,
                'count': len(data_points), 'errors': errors[:10]}

    # ── JSON ──────────────────────────────────────────────────────────────────

    def _parse_json(self, data_bytes: bytes, filename: str = '') -> dict:
        try:
            obj = json.loads(data_bytes.decode('utf-8'))
        except Exception as e:
            return {'device': 'unknown', 'data_points': [], 'count': 0, 'errors': [str(e)]}

        device = self._detect_device(filename, [])
        data_points = []

        # Fitbit: {"activities-heart": [{dateTime, value}], ...}
        if isinstance(obj, dict):
            for key, val in obj.items():
                if not isinstance(val, list):
                    continue
                metric = self._map_metric(key)
                for item in val:
                    if not isinstance(item, dict):
                        continue
                    dt = self._extract_datetime(item)
                    value = self._extract_numeric(item)
                    if dt and value is not None:
                        m = metric or 'other'
                        data_points.append({'metric': m, 'value': value,
                                            'unit': METRIC_UNITS.get(m, ''), 'recorded_at': dt.isoformat()})
        # Generic array: [{date, steps, heart_rate, ...}]
        elif isinstance(obj, list):
            for item in obj:
                if not isinstance(item, dict):
                    continue
                dt = self._extract_datetime(item)
                if not dt:
                    continue
                for k, v in item.items():
                    metric = self._map_metric(k)
                    if not metric:
                        continue
                    try:
                        value = float(v)
                    except (ValueError, TypeError):
                        continue
                    data_points.append({'metric': metric, 'value': value,
                                        'unit': METRIC_UNITS.get(metric, ''), 'recorded_at': dt.isoformat()})

        if device == 'unknown':
            device = 'fitbit' if isinstance(obj, dict) else 'unknown'
        return {'device': device, 'data_points': data_points, 'count': len(data_points), 'errors': []}

    def _extract_numeric(self, item: dict):
        """Pull the first numeric-looking value out of a Fitbit-style {dateTime, value} dict."""
        v = item.get('value')
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            # e.g. Fitbit heart rate: {"bpm": 72, "confidence": 2}
            for sub in ('bpm', 'value', 'count'):
                if sub in v:
                    try:
                        return float(v[sub])
                    except (ValueError, TypeError):
                        pass
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    # ── Apple Health XML ──────────────────────────────────────────────────────

    _AH_TYPE_MAP = {
        'HKQuantityTypeIdentifierHeartRate':              ('heart_rate',  'bpm'),
        'HKQuantityTypeIdentifierStepCount':              ('steps',       'steps'),
        'HKQuantityTypeIdentifierActiveEnergyBurned':     ('calories',    'kcal'),
        'HKQuantityTypeIdentifierDistanceWalkingRunning': ('distance',    'km'),
        'HKQuantityTypeIdentifierOxygenSaturation':       ('blood_oxygen','%'),
        'HKQuantityTypeIdentifierBodyMass':               ('weight',      'kg'),
        'HKQuantityTypeIdentifierBodyTemperature':        ('temperature', '°C'),
        'HKQuantityTypeIdentifierRespiratoryRate':        ('respiratory_rate', 'bpm'),
        'HKCategoryTypeIdentifierSleepAnalysis':          ('sleep',       'h'),
    }

    _XML_MAX_BYTES = 50 * 1024 * 1024  # 50 MB — Apple Health exports can be hundreds of MB

    def _parse_xml(self, data_bytes: bytes, filename: str = '') -> dict:
        if len(data_bytes) > self._XML_MAX_BYTES:
            return {
                'device': 'apple_health', 'data_points': [], 'count': 0,
                'errors': [
                    f'Apple Health export exceeds the '
                    f'{self._XML_MAX_BYTES // (1024 * 1024)} MB limit. '
                    'Export a shorter date range and re-upload.'
                ],
            }

        device = 'apple_health'
        data_points, errors = [], []

        # Use stdlib iterparse for streaming — defusedxml's iterparse is
        # deprecated; the 50 MB cap above provides the size-bomb protection.
        from xml.etree.ElementTree import iterparse as _iterparse
        try:
            for _event, elem in _iterparse(io.BytesIO(data_bytes), events=('end',)):
                if elem.tag != 'Record':
                    elem.clear()
                    continue
                rtype = elem.get('type', '')
                if rtype in self._AH_TYPE_MAP:
                    metric, unit = self._AH_TYPE_MAP[rtype]
                    start_raw = elem.get('startDate') or elem.get('creationDate', '')
                    value_str = elem.get('value')
                    if start_raw and value_str is not None:
                        try:
                            dt    = datetime.strptime(start_raw[:19], '%Y-%m-%d %H:%M:%S')
                            value = float(value_str)
                            data_points.append({'metric': metric, 'value': value,
                                                'unit': unit, 'recorded_at': dt.isoformat()})
                        except (ValueError, TypeError):
                            pass
                elem.clear()
        except Exception as e:
            return {'device': 'apple_health', 'data_points': [], 'count': 0, 'errors': [str(e)]}

        return {'device': device, 'data_points': data_points, 'count': len(data_points), 'errors': errors}

    def _detect_device(self, filename: str, headers: list) -> str:
        fname = filename.lower()
        if 'fitbit' in fname:
            return 'fitbit'
        if 'garmin' in fname:
            return 'garmin'
        if 'oura' in fname:
            return 'oura'
        if 'apple' in fname or 'health' in fname:
            return 'apple_health'
        header_str = ' '.join(h.lower() for h in headers)
        if 'fitbit' in header_str:
            return 'fitbit'
        if 'garmin' in header_str:
            return 'garmin'
        return 'unknown'

    def _parse_row(self, row: dict, headers: list, device: str) -> list:
        points = []

        # Find datetime column
        dt = self._extract_datetime(row)
        if dt is None:
            return []

        for col, raw_val in row.items():
            if not raw_val or not raw_val.strip():
                continue

            metric = self._map_metric(col)
            if metric is None:
                continue

            try:
                value = float(raw_val.replace(',', '.').strip())
            except (ValueError, AttributeError):
                continue

            # Convert units if needed
            value, unit = self._normalize_value(metric, value, col.lower())

            points.append({
                'metric': metric,
                'value': value,
                'unit': unit,
                'recorded_at': dt.isoformat(),
            })

        return points

    def _extract_datetime(self, row: dict):
        date_keys = ['datetime', 'date', 'timestamp', 'time', 'start_time',
                     'Date', 'DateTime', 'Timestamp', 'Time']
        for key in date_keys:
            if key in row and row[key]:
                return self._parse_dt(row[key])
        # Try any column that looks like a date
        for key, val in row.items():
            if 'date' in key.lower() or 'time' in key.lower():
                parsed = self._parse_dt(val)
                if parsed:
                    return parsed
        return None

    def _parse_dt(self, val: str, locale_hint: str | None = None):
        """
        Parse a datetime string from wearable data.
        Uses _parse_date_string for the date component so both wearable and
        text/PDF parsers share the same locale-aware disambiguation logic.
        """
        if not val:
            return None
        val = val.strip()

        # ISO datetime formats are always unambiguous (Y-M-D order is explicit)
        for fmt in (
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%Y-%m-%d',
        ):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue

        # Non-ISO: separate the date part from an optional time component, then
        # run the unified date parser (which handles d/m vs m/d disambiguation).
        dt_m = re.match(
            r'^(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})'
            r'(?:[ T](\d{1,2}:\d{2}(?::\d{2})?))?$',
            val,
        )
        if dt_m:
            date_part = _parse_date_string(dt_m.group(1), locale_hint=locale_hint)
            if date_part:
                time_str = dt_m.group(2)
                if time_str:
                    time_fmt = '%H:%M:%S' if time_str.count(':') == 2 else '%H:%M'
                    try:
                        return datetime.strptime(f'{date_part} {time_str}',
                                                 f'%Y-%m-%d {time_fmt}')
                    except ValueError:
                        pass
                try:
                    return datetime.strptime(date_part, '%Y-%m-%d')
                except ValueError:
                    pass

        return None

    def _map_metric(self, col: str) -> str | None:
        key = col.lower().strip()
        for pattern, metric in METRIC_COLUMN_MAP.items():
            if pattern in key:
                return metric
        return None

    def _normalize_value(self, metric: str, value: float, col_lower: str):
        unit = METRIC_UNITS.get(metric, '')
        if metric == 'weight':
            # Explicit lbs column, OR value > 350 with no kg hint → almost certainly pounds
            # (350 kg would be an extreme medical outlier; 350 lbs ≈ 159 kg is plausible)
            is_lbs = 'lb' in col_lower or (value > 350 and 'kg' not in col_lower)
            if is_lbs:
                value = round(value * 0.453592, 2)
        if metric == 'temperature':
            # Explicit °F column, OR value > 50 → must be Fahrenheit (normal body temp
            # in Celsius peaks around 42–43°C; 50°C is lethal hyperthermia)
            is_fahrenheit = 'fahrenheit' in col_lower or 'f' in col_lower or value > 50
            if is_fahrenheit:
                value = round((value - 32) * 5 / 9, 2)
        if metric == 'sleep' and value > 24:
            value = round(value / 60, 2)
        return value, unit


WearableCSVParser = WearableParser  # backward-compat alias


# ─── Plain Text Parser ───────────────────────────────────────────────────────

class TextParser:
    """Parse free-form pasted medical text and structure it with Gemini."""

    def parse(self, text: str) -> dict:
        result = {
            'raw_text': text,
            'structured': None,
        }
        if text.strip():
            result['structured'] = self._structure_with_ai(text)
        return result

    def _structure_with_ai(self, text: str) -> dict | None:
        return _structure_medical_text_with_ai(text)


# ─── PDF Parser ──────────────────────────────────────────────────────────────

class PDFParser:
    """Extract text from PDF and optionally structure it with Gemini."""

    _MAX_PAGES = 100
    _MAX_BYTES = 20 * 1024 * 1024  # 20 MB — defence against decompression bombs

    # Column-role detection patterns (English + Finnish lab report headers)
    _HEADER_RE = {
        'name':  re.compile(r'test|param|analyte|component|description|exam|tutkimus|testi', re.I),
        'value': re.compile(r'result|value|tulos|arvo|mittaustulos', re.I),
        'unit':  re.compile(r'unit|yksikkö', re.I),
        'ref':   re.compile(r'ref|normal|range|viite|interval|expected|limit', re.I),
        'flag':  re.compile(r'flag|status|interpr|abn|h/?l|poikkeav', re.I),
    }

    # ── Entry point ───────────────────────────────────────────────────────────

    def parse(self, pdf_bytes: bytes, use_ai: bool = True) -> dict:
        if len(pdf_bytes) > self._MAX_BYTES:
            return {'error': f'PDF exceeds the {self._MAX_BYTES // (1024*1024)} MB size limit.'}

        try:
            import pdfplumber
        except ImportError:
            return {'error': 'pdfplumber not installed', 'text': ''}

        text_pages = []
        table_lab_values = []

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) > self._MAX_PAGES:
                return {
                    'error': (
                        f'PDF has {len(pdf.pages)} pages; maximum allowed is {self._MAX_PAGES}. '
                        'Please split the document and upload each part separately.'
                    )
                }
            for page in pdf.pages:
                # Table extraction — more accurate than flat text for multi-column lab reports
                try:
                    for table in (page.extract_tables() or []):
                        table_lab_values.extend(self._parse_table(table))
                except Exception as exc:
                    logger.debug('Table extraction skipped on page: %s', exc)

                # Always extract flat text too — needed for date, title, AI metadata
                page_text = page.extract_text()
                if page_text:
                    text_pages.append(page_text)

        full_text = '\n\n'.join(text_pages)

        result = {
            'raw_text':  full_text,
            'page_count': len(text_pages),
            'structured': None,
        }

        if use_ai and (full_text or table_lab_values):
            result['structured'] = self._structure_with_ai(
                full_text, table_lab_values=table_lab_values
            )

        return result

    # ── Table extraction ──────────────────────────────────────────────────────

    def _parse_table(self, table: list) -> list:
        """
        Parse a pdfplumber table (list-of-rows, each row a list-of-cells) into
        lab value dicts.  Non-numeric value cells are skipped automatically, so
        demographic tables and title rows produce no output.
        """
        if not table:
            return []

        # Normalise: None → empty string
        clean = [[str(cell or '').strip() for cell in row] for row in table]
        ncols = max(len(row) for row in clean) if clean else 0
        if ncols < 2:
            return []

        col_map = self._detect_columns(clean[0], ncols)
        start   = 1 if col_map.pop('_header', False) else 0

        results = []
        for row in clean[start:]:
            while len(row) < ncols:
                row.append('')
            lv = self._row_to_lab_value(row, col_map)
            if lv:
                results.append(lv)
        return results

    def _detect_columns(self, header_row: list, ncols: int) -> dict:
        """
        Map column roles from the header row if it looks like a header,
        otherwise assign roles positionally.
        """
        col_map   = {}
        is_header = False
        for i, cell in enumerate(header_row):
            for role, pat in self._HEADER_RE.items():
                if role not in col_map and pat.search(cell):
                    col_map[role] = i
                    is_header     = True

        col_map['_header'] = is_header
        if not is_header:
            # Positional defaults: name=0, value=1, unit=2, ref=3, flag=4
            for i, role in enumerate(['name', 'value', 'unit', 'ref', 'flag'][:ncols]):
                col_map.setdefault(role, i)

        return col_map

    def _row_to_lab_value(self, row: list, col_map: dict) -> dict | None:
        """Convert one table row to a lab value dict, or None if not a data row."""
        name_i  = col_map.get('name',  0)
        value_i = col_map.get('value', 1)
        unit_i  = col_map.get('unit')
        ref_i   = col_map.get('ref')
        flag_i  = col_map.get('flag')

        name      = row[name_i]  if name_i  < len(row) else ''
        value_str = row[value_i] if value_i < len(row) else ''

        if not name or not value_str:
            return None

        # Skip rows whose name cell looks like a column header
        if any(pat.search(name) for pat in self._HEADER_RE.values()):
            return None

        # Name must contain at least one letter and be a reasonable length
        if not any(c.isalpha() for c in name) or not (1 < len(name) <= 100):
            return None

        # Strip comparison operators (<5.0, ≥2) before float parsing
        value_clean = value_str.replace(',', '.').strip().lstrip('<>≤≥~')
        try:
            float(value_clean)
        except ValueError:
            return None

        unit      = row[unit_i].strip() if unit_i is not None and unit_i < len(row) else ''
        ref_range = row[ref_i].strip()  if ref_i  is not None and ref_i  < len(row) else ''
        flag      = row[flag_i].strip() if flag_i is not None and flag_i < len(row) else ''
        is_abnormal = flag.upper() in {'H', 'L', 'A', 'HH', 'LL', 'HIGH', 'LOW', 'ABNORMAL', '*', '+'}

        return {
            'name':        name,
            'value':       value_clean,
            'unit':        unit,
            'ref_range':   ref_range,
            'is_abnormal': is_abnormal,
        }

    # ── AI structuring ────────────────────────────────────────────────────────

    def _structure_with_ai(self, text: str,
                           table_lab_values: list | None = None) -> dict | None:
        return _structure_medical_text_with_ai(text, table_lab_values=table_lab_values)

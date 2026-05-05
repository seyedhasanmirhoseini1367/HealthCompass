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
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

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
            return val


# ─── Wearable CSV ────────────────────────────────────────────────────────────

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


class WearableCSVParser:
    """Parse wearable device CSV exports into structured data points."""

    def parse(self, csv_bytes: bytes, filename: str = '') -> dict:
        try:
            text = csv_bytes.decode('utf-8-sig')  # handles BOM
        except UnicodeDecodeError:
            text = csv_bytes.decode('latin-1')

        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []

        # Detect device type from filename or headers
        device = self._detect_device(filename, headers)

        data_points = []
        errors = []

        for i, row in enumerate(reader):
            try:
                points = self._parse_row(row, headers, device)
                data_points.extend(points)
            except Exception as e:
                errors.append(f'Row {i+2}: {e}')

        return {
            'device': device,
            'data_points': data_points,
            'count': len(data_points),
            'errors': errors[:10],
        }

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

    def _parse_dt(self, val: str):
        formats = [
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%Y-%m-%d',
            '%m/%d/%Y %H:%M:%S',
            '%m/%d/%Y',
            '%d/%m/%Y',
            '%d.%m.%Y',
        ]
        val = val.strip()
        for fmt in formats:
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
        return None

    def _map_metric(self, col: str) -> str | None:
        key = col.lower().strip()
        for pattern, metric in METRIC_COLUMN_MAP.items():
            if pattern in key:
                return metric
        return None

    def _normalize_value(self, metric: str, value: float, col_lower: str):
        unit = METRIC_UNITS.get(metric, '')
        # Convert lbs to kg
        if metric == 'weight' and 'lb' in col_lower:
            value = round(value * 0.453592, 2)
        # Convert Fahrenheit to Celsius
        if metric == 'temperature' and ('f' in col_lower or 'fahrenheit' in col_lower):
            value = round((value - 32) * 5/9, 2)
        # Convert minutes to hours for sleep
        if metric == 'sleep' and value > 24:
            value = round(value / 60, 2)
        return value, unit


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
        from django.conf import settings
        api_key = getattr(settings, 'GEMINI_API_KEY', '')
        if not api_key:
            return None

        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')

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
            return json.loads(raw)
        except Exception as e:
            logger.warning(f'Gemini text structuring failed: {e}')
            return None


# ─── PDF Parser ──────────────────────────────────────────────────────────────

class PDFParser:
    """Extract text from PDF and optionally structure it with Gemini."""

    def parse(self, pdf_bytes: bytes, use_ai: bool = True) -> dict:
        try:
            import pdfplumber
        except ImportError:
            return {'error': 'pdfplumber not installed', 'text': ''}

        text_pages = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_pages.append(page_text)

        full_text = '\n\n'.join(text_pages)

        result = {
            'raw_text': full_text,
            'page_count': len(text_pages),
            'structured': None,
        }

        if use_ai and full_text:
            result['structured'] = self._structure_with_ai(full_text)

        return result

    def _structure_with_ai(self, text: str) -> dict | None:
        from django.conf import settings
        api_key = getattr(settings, 'GEMINI_API_KEY', '')
        if not api_key:
            return None

        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')

            prompt = f"""Extract structured medical data from this document. Return JSON with:
{{
  "record_type": "lab_result|prescription|diagnosis|vaccination|imaging|discharge|other",
  "title": "short descriptive title",
  "date": "YYYY-MM-DD or null",
  "summary": "2-3 sentence summary",
  "lab_values": [{{"name":"","value":"","unit":"","ref_range":"","is_abnormal":false}}],
  "medications": [{{"name":"","dose":"","frequency":"","route":""}}],
  "diagnoses": [{{"code":"","description":""}}],
  "provider": "",
  "facility": ""
}}
Return only valid JSON, no markdown.

DOCUMENT:
{text[:4000]}"""

            response = model.generate_content(prompt)
            raw = response.text.strip()
            # Strip markdown code fences if present
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            return json.loads(raw)
        except Exception as e:
            logger.warning(f'Gemini structuring failed: {e}')
            return None

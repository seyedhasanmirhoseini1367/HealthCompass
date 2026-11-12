"""Finnish Kanta (HL7 CDA) XML exports."""
import logging
from datetime import datetime
from . import ET, DefusedXmlException  # noqa: F401  defusedxml, enforced in __init__

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
        except DefusedXmlException as e:
            # A document defusedxml refuses (entity expansion, external entity,
            # DTD retrieval). Rejection is the point; it is reported through the
            # same {'error': ...} contract `create_from_kanta` already handles so
            # the upload fails cleanly instead of raising a 500 — these
            # exceptions derive from ValueError, not ParseError, so without this
            # branch they would escape the parser entirely.
            logger.warning('Rejected unsafe XML upload: %s', type(e).__name__)
            from healthcompass.observability import Event as OpsEvent, emit as ops_emit
            ops_emit(OpsEvent.UNSAFE_DOCUMENT_REJECTED, error_type=type(e).__name__)
            return {'error': f'Unsafe XML rejected: {type(e).__name__}', 'records': []}

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

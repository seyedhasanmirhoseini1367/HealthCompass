"""
Parsers for medical record formats, one module per format:
  extraction  dates, regex lab values, LLM structuring
  kanta       Finnish Kanta (HL7 CDA) XML
  wearable    device exports
  documents   pasted text and PDF
The public names stay importable from here.

Formats:
- KantaXMLParser: Finnish Kanta XML exports
- WearableCSVParser: CSV exports from Fitbit, Garmin, Oura, Apple Health
- PDFParser: PDF documents using pdfplumber + Gemini for structuring
"""
import logging
# XML hardening is mandatory, never optional.
#
# This previously fell back to `xml.etree.ElementTree` when defusedxml was
# missing, "for local dev". The fallback was silent — no error, no log — and
# defusedxml was absent from the environment where the whole test suite passed,
# so every green run was compatible with the hardening being switched off.
# Measured: stdlib ElementTree expands a three-level nested-entity document from
# 10 to 1000 characters; the standard nine-level form expands to gigabytes, so
# an uploaded Kanta file could exhaust memory. defusedxml raises
# EntitiesForbidden on the same input.
#
# Failing at import is deliberate: a missing security control must stop the
# application, not degrade it quietly. defusedxml is a pinned dependency.
try:
    import defusedxml.ElementTree as ET  # noqa: F401  re-exported to the format modules
    from defusedxml.common import DefusedXmlException  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised via reload in tests
    raise ImportError(
        'defusedxml is required for XML parsing and is not installed. '
        'It protects the Kanta XML upload path against entity-expansion '
        'attacks. Install it (see requirements.txt) rather than removing this '
        'check — the stdlib XML parser is not a safe substitute.'
    ) from exc

logger = logging.getLogger(__name__)

from .extraction import *  # noqa: E402,F401,F403
from .extraction import (_extract_date_regex, _extract_lab_values_regex,  # noqa: E402,F401
                         _parse_date_string, _structure_medical_text_with_ai, _to_iso)
from .kanta import *  # noqa: E402,F401,F403
from .kanta import KANTA_NS, KantaXMLParser, _tag  # noqa: E402,F401
from .wearable import *  # noqa: E402,F401,F403
from .wearable import METRIC_COLUMN_MAP, METRIC_UNITS, WearableCSVParser, WearableParser  # noqa: E402,F401
from .documents import PDFParser, TextParser  # noqa: E402,F401

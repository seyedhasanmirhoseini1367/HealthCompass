"""Pasted text and PDF documents."""
import io
import re
import logging
from apps.medical_records import parsers as _parsers

logger = logging.getLogger(__name__)


# ─── Plain Text Parser ───────────────────────────────────────────────────────

class TextParser:
    """Parse free-form pasted medical text and structure it with Gemini."""

    def parse(self, text: str, use_ai: bool = True) -> dict:
        result = {
            'raw_text': text,
            'structured': None,
        }
        if text.strip():
            result['structured'] = self._structure_with_ai(text, use_ai=use_ai)
        return result

    def _structure_with_ai(self, text: str, use_ai: bool = True) -> dict | None:
        return _parsers._structure_medical_text_with_ai(text, allow_external=use_ai)


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
        return _parsers._structure_medical_text_with_ai(text, table_lab_values=table_lab_values)

# rag_assistant/services/trajectory_service.py
"""
Trajectory Service — temporal clinical trajectory reasoning.

Query flow:
  1. detect_biomarker()    — find canonical biomarker name in query text
  2. _load_trajectory()    — ORM-first: query ParsedLabValue directly,
                             fall back to MedicalChunk text for records
                             that have no structured lab values
  3. _format_trajectory()  — build LLM-ready context with trend analysis
  4. get_chart_data()      — Chart.js-ready dict for the frontend

ORM-first approach eliminates the previous parse-text→chunk→regex pipeline
that re-extracted numbers from already-structured data. Uses canonical_value
(stored in mg/dL or canonical SI unit) for safe cross-record comparison;
original_unit is preserved in metadata for display.
"""
import logging
import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from django.db.models import Q


def _biomarker_alias_match(alias: str, q: str) -> bool:
    if ' ' in alias:
        return alias in q
    return bool(re.search(r'\b' + re.escape(alias) + r'\b', q))

logger = logging.getLogger(__name__)


_CHART_KEYWORDS = [
    'diagram', 'chart', 'graph', 'plot', 'visuali', 'visualise',
    'show me a', 'show my', 'draw', 'display my data', 'show a',
]

# Biomarker canonical name → list of parameter_name substrings to match in DB.
# More specific entries must come before shorter/ambiguous ones.
_BIOMARKERS: Dict[str, List[str]] = {
    'creatinine': [
        'creatinine', 'serum creatinine', 'creat', 'kidney function',
    ],
    'hba1c': [
        'hba1c', 'hemoglobin a1c', 'haemoglobin a1c', 'glycated hemoglobin',
        'glycated haemoglobin', 'a1c',
    ],
    'egfr': [
        'egfr', 'gfr', 'glomerular filtration rate', 'estimated gfr',
    ],
    'glucose': [
        'fasting glucose', 'blood glucose', 'blood sugar', 'glucose',
        'fbs', 'rbs', 'random blood sugar',
    ],
    'cholesterol': [
        'total cholesterol', 'ldl cholesterol', 'hdl cholesterol',
        'ldl', 'hdl', 'cholesterol', 'triglyceride',
    ],
    'hemoglobin': [
        'hemoglobin', 'haemoglobin', 'hgb', 'hb level',
    ],
    'blood_pressure': [
        'systolic', 'diastolic', 'blood pressure', 'bp',
    ],
    'heart_rate': [
        'heart rate', 'resting heart rate', 'pulse rate', 'bpm',
    ],
    'weight': [
        'body weight', 'weight', 'bmi',
    ],
    'tsh': [
        'thyroid stimulating hormone', 'tsh', 'thyroid function',
    ],
    'vitamin_d': [
        '25-hydroxyvitamin d', '25(oh)d', 'vitamin d', 'vit d',
    ],
    'sodium': ['sodium', 'serum sodium', 'na+'],
    'potassium': ['potassium', 'serum potassium', 'k+'],
    'urea': ['blood urea nitrogen', 'bun', 'urea'],
    'wbc': ['white blood cell', 'white blood count', 'wbc', 'leukocyte'],
    'platelets': ['platelet count', 'platelet', 'plt', 'thrombocyte'],
}

# Duplicate keyword list kept for backward compat with callers; the router
# uses its own copy. Single source of truth is planned for QueryUnderstanding.
_TEMPORAL_KEYWORDS = [
    'getting better', 'getting worse', 'is it getting', 'is it improving',
    'is it worsening', 'has it improved', 'has it worsened',
    'over the past', 'over time', 'over the last', 'throughout the year',
    'compared to last', 'compared to before', 'compared to my previous',
    'since last', 'since my last', 'from last',
    'month by month', 'each visit', 'every visit', 'each time',
    'trend', 'trending', 'trajectory', 'progression', 'progress',
    'improving', 'worsening', 'deteriorating', 'declining', 'rising',
    'going up', 'going down', 'increasing', 'decreasing', 'elevated',
    'getting higher', 'getting lower', 'changed', 'change over',
    'how long have', 'how long has', 'timeline', 'history of my',
    'last 6 months', 'last 3 months', 'last year', 'last month',
    'fluctuating', 'stable over', 'consistently', 'pattern',
    'should i be worried', 'is this serious', 'is this concerning',
]


class TrajectoryService:

    def is_temporal_query(self, query: str) -> bool:
        q = query.lower()
        return any(kw in q for kw in _TEMPORAL_KEYWORDS)

    def is_chart_request(self, query: str) -> bool:
        q = query.lower()
        return any(kw in q for kw in _CHART_KEYWORDS)

    def detect_biomarker(self, query: str) -> Optional[str]:
        q = query.lower()
        for canonical, aliases in _BIOMARKERS.items():
            if any(_biomarker_alias_match(alias, q) for alias in aliases):
                return canonical
        return None

    def get_trajectory_context(
        self,
        patient,
        query: str,
    ) -> Tuple[str, List[Dict]]:
        biomarker = self.detect_biomarker(query)
        if not biomarker:
            return self._general_temporal_context(patient)

        aliases = _BIOMARKERS[biomarker]
        trajectory_points, source_chunks = self._load_trajectory(patient, aliases, biomarker)

        if not trajectory_points:
            return '', []

        context = self._format_trajectory(biomarker, trajectory_points)
        return context, source_chunks

    # ── Primary: ORM-based loading from ParsedLabValue ────────────────────────

    def _load_trajectory(
        self,
        patient,
        aliases: List[str],
        biomarker: str,
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Query ParsedLabValue directly — no chunk scanning, no regex.

        Falls back to chunk text scan only when no structured rows exist
        (e.g. the record pre-dates the migration, or the lab result was
        stored as a clinical note without a ParsedLabValue row).
        """
        from apps.medical_records.models import ParsedLabValue
        from apps.medical_records.unit_normalizer import normalize as normalize_unit

        # Build OR filter over all aliases
        q_filter = Q()
        for alias in aliases:
            q_filter |= Q(parameter_name__icontains=alias)

        lab_rows = (
            ParsedLabValue.objects
            .filter(record__patient=patient, unit_known=True)
            .filter(q_filter)
            .select_related('record')
            .order_by('record__record_date', 'measured_at')
        )

        trajectory_points: List[Dict] = []
        seen_record_dates: set = set()

        for row in lab_rows:
            record_date = row.record.record_date
            if not record_date:
                continue

            # One data point per (biomarker, date) — deduplicate by record date
            key = str(record_date)
            if key in seen_record_dates:
                continue
            seen_record_dates.add(key)

            # Use canonical_value when available (post-migration rows).
            # For legacy rows without canonical_value, re-normalize on the fly
            # and skip if the unit is still unrecognised after normalisation.
            if row.canonical_value is not None:
                numeric_value = row.canonical_value
                display_unit  = row.unit           # already canonical unit
                original_unit = row.original_unit or row.unit
            else:
                numeric_value, display_unit, original_unit, unit_ok = normalize_unit(
                    row.parameter_name, row.value, row.unit,
                )
                if not unit_ok:
                    logger.warning(
                        'trajectory: skipping %s %s %s — unit not recognised',
                        row.parameter_name, row.value, row.unit,
                    )
                    continue

            trajectory_points.append({
                'date':          record_date,
                'date_str':      str(record_date),
                'value':         numeric_value,
                'unit':          display_unit,
                'original_unit': original_unit,
                'is_abnormal':   row.is_abnormal,
                'text': (
                    f"{row.parameter_name}: {row.value} {original_unit}"
                    + (f" [ABNORMAL]" if row.is_abnormal else "")
                    + (f" | ref: {row.reference_range}" if row.reference_range else "")
                ),
                'metadata': {
                    'document_title': row.record.title,
                    'document_type':  row.record.record_type,
                    'record_date':    str(record_date),
                    'record_id':      str(row.record.id),
                    'parameter_name': row.parameter_name,
                    'original_unit':  original_unit,
                },
            })

        if trajectory_points:
            trajectory_points.sort(key=lambda x: x['date'])
            source_chunks = [
                {'text': p['text'], 'score': 1.0, 'metadata': p['metadata']}
                for p in trajectory_points
            ]
            return trajectory_points, source_chunks

        # No ParsedLabValue rows found — fall back to chunk text scanning
        logger.debug(
            'TrajectoryService: no ParsedLabValue rows for %s, falling back to chunk scan',
            biomarker,
        )
        return self._load_trajectory_from_chunks(patient, aliases)

    # ── Fallback: chunk text scan (legacy / non-lab records) ─────────────────

    def _load_trajectory_from_chunks(
        self,
        patient,
        aliases: List[str],
    ) -> Tuple[List[Dict], List[Dict]]:
        from apps.rag_assistant.models import MedicalChunk

        all_chunks = MedicalChunk.objects.filter(
            patient=patient
        ).select_related('document', 'document__record')

        matched = []
        for chunk in all_chunks:
            content_lower = chunk.content.lower()
            if not any(alias in content_lower for alias in aliases):
                continue

            meta        = chunk.metadata or {}
            record_date = meta.get('record_date') or ''
            if not record_date:
                try:
                    rd = chunk.document.record.record_date
                    record_date = str(rd) if rd else ''
                except Exception:
                    pass
            if not record_date:
                continue

            try:
                parsed_date = (
                    date.fromisoformat(record_date)
                    if isinstance(record_date, str)
                    else record_date
                )
            except ValueError:
                continue

            matched.append({
                'date':          parsed_date,
                'date_str':      str(record_date),
                'text':          chunk.content,
                'metadata': {
                    'document_id':    str(chunk.document.id),
                    'document_title': chunk.document.title,
                    'document_type':  chunk.document.document_type,
                    'record_date':    str(record_date),
                },
            })

        if not matched:
            return [], []

        # Deduplicate and sort
        seen: set = set()
        deduped: List[Dict] = []
        for item in sorted(matched, key=lambda x: x['date']):
            doc_id = item['metadata']['document_id']
            if doc_id not in seen:
                seen.add(doc_id)
                deduped.append(item)

        # Extract numeric values from text (best-effort)
        trajectory_points = []
        for item in deduped:
            value, unit = self._extract_numeric_from_text(item['text'], aliases)
            trajectory_points.append({**item, 'value': value, 'unit': unit,
                                       'original_unit': unit, 'is_abnormal': False})

        source_chunks = [
            {'text': p['text'], 'score': 1.0, 'metadata': p['metadata']}
            for p in trajectory_points
        ]
        return trajectory_points, source_chunks

    def _extract_numeric_from_text(
        self,
        text: str,
        aliases: List[str],
    ) -> Tuple[Optional[float], str]:
        """Regex fallback — only used when no ParsedLabValue rows exist."""
        for alias in aliases:
            pattern = re.compile(
                re.escape(alias) + r'[^:\n]{0,30}?[:\s=]+(\d+\.?\d*)\s*([a-zA-Z/%µμ]+)?',
                re.IGNORECASE,
            )
            m = pattern.search(text)
            if m:
                try:
                    return float(m.group(1)), (m.group(2) or '').strip()
                except ValueError:
                    pass
        return None, ''

    # ── Format trajectory for LLM ─────────────────────────────────────────────

    def _format_trajectory(self, biomarker: str, points: List[Dict]) -> str:
        display = biomarker.replace('_', ' ').title()
        lines = [
            f"=== TEMPORAL TRAJECTORY: {display.upper()} ===",
            "Ordered chronologically (oldest → newest).",
            "",
        ]

        numeric_series: List[Tuple] = []
        for i, pt in enumerate(points, 1):
            date_label = (
                pt['date'].strftime('%B %Y')
                if isinstance(pt['date'], date) else pt['date_str']
            )
            if pt['value'] is not None:
                orig = pt.get('original_unit', pt['unit'])
                val_str = f"{pt['value']} {pt['unit']}".strip()
                if orig and orig != pt['unit']:
                    val_str += f" (reported as {orig})"
                flag = " [ABNORMAL]" if pt.get('is_abnormal') else ""
                numeric_series.append((pt['date'], pt['value'], pt['unit']))
            else:
                val_str = '(see excerpt below)'
                flag = ''
            lines.append(f"  [{i}] {date_label}: {display} = {val_str}{flag}")

        lines.append("")

        if len(numeric_series) >= 2:
            lines.append(f"TREND ANALYSIS: {self._compute_trend(numeric_series)}")
        elif len(numeric_series) == 1:
            lines.append("TREND ANALYSIS: Only one numeric reading available; trend cannot be computed.")

        lines.append("")
        lines.append("=== SOURCE RECORD EXCERPTS ===")
        for i, pt in enumerate(points, 1):
            date_label = (
                pt['date'].strftime('%B %Y')
                if isinstance(pt['date'], date) else pt['date_str']
            )
            excerpt = pt['text'][:500].strip()
            lines.append(f"\n[{i}] {date_label}:\n{excerpt}")

        return "\n".join(lines)

    def _compute_trend(self, values: List[Tuple]) -> str:
        import math

        base      = values[0][0]
        x_days    = [(v[0] - base).days for v in values]
        y         = [v[1] for v in values]
        unit      = values[-1][2] or ''
        n         = len(y)
        first_val = y[0]
        last_val  = y[-1]
        span_days = x_days[-1]

        if n >= 2:
            mean_y  = sum(y) / n
            sigma_t = math.sqrt(sum((yi - mean_y) ** 2 for yi in y) / (n - 1))
        else:
            sigma_t = 1.0
        sigma_t = max(sigma_t, 1e-6)

        w_s         = min(span_days / 365.0, 1.0)
        delta_raw   = last_val - first_val
        delta_score = (delta_raw / sigma_t) * w_s

        sx  = sum(x_days)
        sy  = sum(y)
        sxy = sum(xi * yi for xi, yi in zip(x_days, y))
        sxx = sum(xi * xi for xi in x_days)
        den = n * sxx - sx * sx
        slope_per_day = (n * sxy - sx * sy) / den if den != 0 else 0.0

        pct_change = (delta_raw / first_val * 100) if first_val else 0.0
        sign       = '+' if pct_change >= 0 else ''
        span_str   = f"{span_days // 30} months" if span_days >= 30 else f"{span_days} days"
        direction  = (
            "INCREASING" if slope_per_day > 0 else
            ("DECREASING" if slope_per_day < 0 else "STABLE")
        )
        abs_delta = abs(delta_score)
        significance = (
            "clinically significant change" if abs_delta >= 2.0
            else ("notable change" if abs_delta >= 1.0
                  else "change within this patient's normal variability")
        )

        return (
            f"Values are {direction} over {span_str}. "
            f"First: {first_val} {unit} → Latest: {last_val} {unit}. "
            f"Total change: {sign}{pct_change:.1f}% "
            f"(slope: {sign}{slope_per_day * 30:.3f} {unit}/month).\n"
            f"Formal Δ(D,t,s) = {delta_score:+.3f}  "
            f"[σ_t={sigma_t:.3f} {unit}, w(s)={w_s:.2f}, span={span_days}d] "
            f"— {significance}."
        )

    # ── Chart data ─────────────────────────────────────────────────────────────

    def get_chart_data(self, patient, query: str) -> Optional[Dict]:
        biomarker = self.detect_biomarker(query)
        if not biomarker:
            best, best_count = None, 0
            for canonical, aliases in _BIOMARKERS.items():
                pts, _ = self._load_trajectory(patient, aliases, canonical)
                count = sum(1 for p in pts if p['value'] is not None)
                if count > best_count:
                    best, best_count = canonical, count
            if not best:
                return None
            biomarker = best

        aliases = _BIOMARKERS[biomarker]
        trajectory_points, _ = self._load_trajectory(patient, aliases, biomarker)

        numeric = [p for p in trajectory_points if p['value'] is not None]
        if len(numeric) < 2:
            return None

        labels = [
            p['date'].strftime('%b %Y') if isinstance(p['date'], date) else p['date_str']
            for p in numeric
        ]
        values = [p['value'] for p in numeric]
        unit   = numeric[-1]['unit'] or ''

        trend_summary  = self._compute_trend([(p['date'], p['value'], p['unit']) for p in numeric])
        direction      = ('INCREASING' if 'INCREASING' in trend_summary
                          else ('DECREASING' if 'DECREASING' in trend_summary else 'STABLE'))
        first_v, last_v = values[0], values[-1]
        pct_change      = ((last_v - first_v) / first_v * 100) if first_v else 0.0

        slope_match     = re.search(r'slope:\s*[+\-]?(\d+\.?\d*)', trend_summary)
        slope_per_month = float(slope_match.group(1)) if slope_match else 0.0
        if 'DECREASING' in trend_summary and slope_per_month > 0:
            slope_per_month = -slope_per_month

        delta_match = re.search(r'Δ\(D,t,s\)\s*=\s*([+\-]?\d+\.?\d*)', trend_summary)
        delta_score = float(delta_match.group(1)) if delta_match else 0.0

        ref_low, ref_high = self._get_ref_band(biomarker, unit)

        return {
            'biomarker':       biomarker,
            'display_name':    biomarker.replace('_', ' ').title(),
            'unit':            unit,
            'labels':          labels,
            'values':          values,
            'trend_direction': direction,
            'pct_change':      round(pct_change, 1),
            'slope_per_month': round(slope_per_month, 3),
            'delta_score':     round(delta_score, 3),
            'reference_low':   ref_low,
            'reference_high':  ref_high,
        }

    def _get_ref_band(self, biomarker: str, unit: str) -> Tuple[Optional[float], Optional[float]]:
        u = unit.lower()
        if biomarker == 'creatinine':
            if 'umol' in u or 'µmol' in u or 'μmol' in u:
                return (44, 115)
            return (0.5, 1.2)       # mg/dL default (post-normalization)
        if biomarker == 'hba1c':
            if '%' in u or 'percent' in u or 'ngsp' in u:
                return (None, 6.5)
            return (None, 48)
        if biomarker == 'egfr':
            return (60, None)
        if biomarker == 'glucose':
            if 'mg' in u:
                return (70, 110)
            return (3.9, 6.1)
        if biomarker == 'cholesterol':
            if 'mg' in u:
                return (None, 200)
            return (None, 5.0)
        if biomarker == 'tsh':
            return (0.4, 4.0)
        if biomarker == 'vitamin_d':
            if 'ng' in u:
                return (20, 50)
            return (50, 125)
        if biomarker == 'hemoglobin':
            if 'g/dl' in u or 'gdl' in u:
                return (11.7, 17.5)
            return (117, 175)
        if biomarker == 'blood_pressure':
            return (None, 140)
        return (None, None)

    # ── Fallback: no specific biomarker ───────────────────────────────────────

    def _general_temporal_context(self, patient) -> Tuple[str, List[Dict]]:
        from apps.rag_assistant.models import MedicalChunk

        all_chunks = MedicalChunk.objects.filter(
            patient=patient
        ).select_related('document')

        items = []
        for chunk in all_chunks:
            meta        = chunk.metadata or {}
            record_date = meta.get('record_date') or ''
            if not record_date:
                continue
            try:
                d = date.fromisoformat(str(record_date))
                items.append({'date': d, 'date_str': str(record_date), 'chunk': chunk})
            except ValueError:
                pass

        if not items:
            return '', []

        items.sort(key=lambda x: x['date'])
        seen:     set  = set()
        selected: List = []
        for item in reversed(items):
            doc_id = str(item['chunk'].document.id)
            if doc_id not in seen:
                seen.add(doc_id)
                selected.append(item)
            if len(selected) == 8:
                break
        selected.reverse()

        lines = ["=== CHRONOLOGICAL HEALTH RECORD TIMELINE ===\n"]
        source_chunks = []
        for item in selected:
            chunk = item['chunk']
            lines.append(f"[{item['date_str']}] {chunk.document.title}:\n{chunk.content[:300]}\n")
            source_chunks.append({
                'text':  chunk.content,
                'score': 1.0,
                'metadata': {
                    'document_id':    str(chunk.document.id),
                    'document_title': chunk.document.title,
                    'document_type':  chunk.document.document_type,
                    'record_date':    item['date_str'],
                },
            })

        return "\n".join(lines), source_chunks

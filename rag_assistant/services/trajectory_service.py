# rag_assistant/services/trajectory_service.py
"""
Trajectory Service — implements temporal clinical trajectory reasoning.

Problem (Gap 1 from PhD proposal):
  Standard similarity-based retrieval treats all records as temporally flat.
  A query like "is my kidney function getting worse?" retrieves chunks ranked
  by relevance, not time, so the LLM cannot reason about direction or slope.

Solution:
  1. Detect temporal intent in the query (_TEMPORAL_KEYWORDS).
  2. Identify the biomarker being asked about (_BIOMARKERS map).
  3. Load ALL patient chunks that mention that biomarker.
  4. Sort strictly ascending by record_date.
  5. Extract numeric values and compute a linear slope.
  6. Return a structured context string with explicit temporal markers
     so the LLM can reason about direction, magnitude, and rate of change.

This directly implements the Δ(D,t,s) temporal component of the unified
Clinical Retrievability Score:
    Score(q,D) = α·BM25 + β·cos + γ·Δ(D,t,s) + δ·Context(D,i)
"""
import logging
import re
from datetime import date
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Temporal intent keyword list ───────────────────────────────────────────────
# These phrases signal the user wants reasoning about change over time.
# Sorted roughly by specificity (more specific phrases first to avoid
# false positives from very short words).

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

_CHART_KEYWORDS = [
    'diagram', 'chart', 'graph', 'plot', 'visuali', 'visualise',
    'show me a', 'show my', 'draw', 'display my data', 'show a',
]


# ── Biomarker canonical name → synonym list ────────────────────────────────────
# Keys are canonical snake_case names; values are text substrings to search for.
# Ordered from most specific to least specific within each entry.

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


class TrajectoryService:
    """
    Detects temporal queries, identifies biomarkers, and builds ordered
    clinical trajectories for trend-aware RAG retrieval.
    """

    # ── Public API ─────────────────────────────────────────────────────────────

    def is_temporal_query(self, query: str) -> bool:
        """Return True when the query asks about change/trend over time."""
        q = query.lower()
        return any(kw in q for kw in _TEMPORAL_KEYWORDS)

    def is_chart_request(self, query: str) -> bool:
        """Return True when the query explicitly asks for a chart/diagram/graph."""
        q = query.lower()
        return any(kw in q for kw in _CHART_KEYWORDS)

    def detect_biomarker(self, query: str) -> Optional[str]:
        """
        Return the canonical biomarker name that appears in the query.
        Returns None if no known biomarker is mentioned.
        Checks more-specific aliases before shorter ones to prevent partial
        matches (e.g. 'ldl cholesterol' matched before bare 'ldl').
        """
        q = query.lower()
        for canonical, aliases in _BIOMARKERS.items():
            # Aliases are already ordered specific → general
            if any(alias in q for alias in aliases):
                return canonical
        return None

    def get_trajectory_context(
        self,
        patient,
        query: str,
    ) -> Tuple[str, List[Dict]]:
        """
        Build trajectory-aware context for a temporally directed query.

        Returns:
            (context_string, source_chunks)
            context_string — ready for injection into the LLM prompt
            source_chunks  — list of {'text', 'score', 'metadata'} dicts
                             compatible with generation_service._build_sources()
        """
        biomarker = self.detect_biomarker(query)
        if not biomarker:
            # No specific biomarker — return chronological overview of all records
            return self._general_temporal_context(patient)

        aliases = _BIOMARKERS[biomarker]
        trajectory_points, source_chunks = self._load_trajectory(patient, aliases)

        if not trajectory_points:
            # Biomarker named but no records found → let standard retrieval handle
            return '', []

        context = self._format_trajectory(biomarker, trajectory_points)
        return context, source_chunks

    # ── Internal: load & order trajectory ─────────────────────────────────────

    def _load_trajectory(
        self,
        patient,
        aliases: List[str],
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Load all MedicalChunk rows for *patient* whose content mentions any of
        *aliases*, sort ascending by record_date, extract numeric values.

        Returns:
            (trajectory_points, source_chunks)
        """
        from rag_assistant.models import MedicalChunk

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

            # Fall back to the linked record's date if metadata is missing
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
                'date':       parsed_date,
                'date_str':   str(record_date),
                'text':       chunk.content,
                'metadata': {
                    'document_id':    str(chunk.document.id),
                    'document_title': chunk.document.title,
                    'document_type':  chunk.document.document_type,
                    'record_date':    str(record_date),
                },
            })

        if not matched:
            return [], []

        # Deduplicate by document_id + date (keep first chunk per document)
        seen_docs: set = set()
        deduped = []
        for item in sorted(matched, key=lambda x: x['date']):
            doc_id = item['metadata']['document_id']
            if doc_id not in seen_docs:
                seen_docs.add(doc_id)
                deduped.append(item)

        # Extract numeric values
        trajectory_points = []
        for item in deduped:
            value, unit = self._extract_numeric(item['text'], aliases)
            trajectory_points.append({
                'date':     item['date'],
                'date_str': item['date_str'],
                'value':    value,
                'unit':     unit,
                'text':     item['text'],
                'metadata': item['metadata'],
            })

        source_chunks = [
            {'text': p['text'], 'score': 1.0, 'metadata': p['metadata']}
            for p in trajectory_points
        ]

        return trajectory_points, source_chunks

    # ── Internal: numeric value extraction ────────────────────────────────────

    def _extract_numeric(
        self,
        text: str,
        aliases: List[str],
    ) -> Tuple[Optional[float], str]:
        """
        Scan *text* for '<alias>: <number> <unit>' patterns.
        Returns the first match found, or (None, '').
        """
        for alias in aliases:
            pattern = re.compile(
                re.escape(alias) + r'[^:\n]{0,30}?[:\s=]+(\d+\.?\d*)\s*([a-zA-Z/%]+)?',
                re.IGNORECASE,
            )
            m = pattern.search(text)
            if m:
                try:
                    value = float(m.group(1))
                    unit  = (m.group(2) or '').strip()
                    return value, unit
                except ValueError:
                    pass
        return None, ''

    # ── Internal: format trajectory for LLM ───────────────────────────────────

    def _format_trajectory(
        self,
        biomarker: str,
        points: List[Dict],
    ) -> str:
        """
        Produce a structured context string that makes temporal reasoning easy.

        Example output:
            === TEMPORAL TRAJECTORY: CREATININE ===
            Ordered chronologically (oldest → newest).

            [1] January 2025: Creatinine = 0.9 mg/dL
            [2] March 2025:   Creatinine = 1.1 mg/dL
            ...

            TREND ANALYSIS: Values are INCREASING over 11 months.
            First reading: 0.9 mg/dL, Latest reading: 2.1 mg/dL.
            Total change: +133.3% (slope: +0.109 mg/dL/month).

            === SOURCE RECORD EXCERPTS ===
            [1] January 2025:
            Lab result: Annual Check-Up Lab Results — 2025-01-15
            ...
        """
        display = biomarker.replace('_', ' ').title()
        lines   = [
            f"=== TEMPORAL TRAJECTORY: {display.upper()} ===",
            "Ordered chronologically (oldest \u2192 newest).",
            "",
        ]

        numeric_series: List[Tuple] = []
        for i, pt in enumerate(points, 1):
            date_label = (
                pt['date'].strftime('%B %Y')
                if isinstance(pt['date'], date) else pt['date_str']
            )
            if pt['value'] is not None:
                val_str = f"{pt['value']} {pt['unit']}".strip()
                numeric_series.append((pt['date'], pt['value'], pt['unit']))
            else:
                val_str = '(see excerpt below)'
            lines.append(f"  [{i}] {date_label}: {display} = {val_str}")

        lines.append("")

        # Trend summary
        if len(numeric_series) >= 2:
            trend = self._compute_trend(numeric_series)
            lines.append(f"TREND ANALYSIS: {trend}")
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
        """
        Compute formal Δ(D,t,s) = sign(vₙ−v₁) · |vₙ−v₁| / σ_t · w(s)
        plus a linear regression slope, and return a human-readable summary.

        Components:
            σ_t  — patient-specific standard deviation of all readings
                   (normalises the change relative to *this* patient's variability,
                    not a population average)
            w(s) — scope weight: confidence in the trend scales with observation
                   window length (w=1.0 after 365 days, w=0 for a single day)
            Δ    — the normalised, scope-weighted temporal trajectory score
                   |Δ| ≥ 2.0  → clinically significant
                   |Δ| ≥ 1.0  → notable change
                   |Δ| < 1.0  → within this patient's normal variability
        """
        import math

        base      = values[0][0]
        x_days    = [(v[0] - base).days for v in values]
        y         = [v[1] for v in values]
        unit      = values[-1][2] or ''
        n         = len(y)
        first_val = y[0]
        last_val  = y[-1]
        span_days = x_days[-1]

        # ── σ_t : patient-specific standard deviation ─────────────────────────
        if n >= 2:
            mean_y  = sum(y) / n
            sigma_t = math.sqrt(sum((yi - mean_y) ** 2 for yi in y) / (n - 1))
        else:
            sigma_t = 1.0
        sigma_t = max(sigma_t, 1e-6)          # guard against zero-variance series

        # ── w(s) : scope weight — confidence scales with observation window ───
        # Reaches full weight (1.0) at 365 days; short windows are discounted.
        w_s = min(span_days / 365.0, 1.0)

        # ── Δ(D,t,s) : normalised temporal trajectory score ───────────────────
        delta_raw   = last_val - first_val
        delta_score = (delta_raw / sigma_t) * w_s   # signed; positive = rising

        # ── Linear regression slope (per month) ───────────────────────────────
        sx  = sum(x_days)
        sy  = sum(y)
        sxy = sum(xi * yi for xi, yi in zip(x_days, y))
        sxx = sum(xi * xi for xi in x_days)
        den = n * sxx - sx * sx
        slope_per_day = (n * sxy - sx * sy) / den if den != 0 else 0.0

        # ── Human-readable components ─────────────────────────────────────────
        pct_change = (delta_raw / first_val * 100) if first_val else 0.0
        sign       = '+' if pct_change >= 0 else ''
        span_str   = f"{span_days // 30} months" if span_days >= 30 else f"{span_days} days"

        direction = (
            "INCREASING" if slope_per_day > 0 else
            ("DECREASING" if slope_per_day < 0 else "STABLE")
        )

        abs_delta = abs(delta_score)
        if abs_delta >= 2.0:
            significance = "clinically significant change"
        elif abs_delta >= 1.0:
            significance = "notable change"
        else:
            significance = "change within this patient's normal variability"

        return (
            f"Values are {direction} over {span_str}. "
            f"First: {first_val} {unit} → Latest: {last_val} {unit}. "
            f"Total change: {sign}{pct_change:.1f}% "
            f"(slope: {sign}{slope_per_day * 30:.3f} {unit}/month).\n"
            f"Formal Δ(D,t,s) = {delta_score:+.3f}  "
            f"[σ_t={sigma_t:.3f} {unit}, w(s)={w_s:.2f}, span={span_days}d] "
            f"— {significance}."
        )

    # ── Chart data export ──────────────────────────────────────────────────────

    def get_chart_data(self, patient, query: str) -> Optional[Dict]:
        """
        Return a Chart.js-ready dict for the biomarker trend in *query*, or None
        if no plottable data is available.

        Shape:
          {
            'biomarker':       'creatinine',
            'display_name':    'Creatinine',
            'unit':            'µmol/L',
            'labels':          ['Jan 2024', 'Nov 2024', 'Mar 2025'],
            'values':          [118.0, 138.0, 145.0],
            'trend_direction': 'INCREASING',          # INCREASING | DECREASING | STABLE
            'pct_change':      23.0,
            'slope_per_month': 2.52,
            'delta_score':     1.85,
            'reference_low':   None,                  # optional clinical reference band
            'reference_high':  None,
          }
        """
        biomarker = self.detect_biomarker(query)

        if not biomarker:
            # No specific biomarker named — pick the one with the most numeric readings
            best, best_count = None, 0
            for canonical, aliases in _BIOMARKERS.items():
                pts, _ = self._load_trajectory(patient, aliases)
                numeric_count = sum(1 for p in pts if p['value'] is not None)
                if numeric_count > best_count:
                    best, best_count = canonical, numeric_count
            if not best:
                return None
            biomarker = best

        aliases = _BIOMARKERS[biomarker]
        trajectory_points, _ = self._load_trajectory(patient, aliases)

        # Need at least 2 numeric points to draw a meaningful chart
        numeric = [p for p in trajectory_points if p['value'] is not None]
        if len(numeric) < 2:
            return None

        labels = [
            p['date'].strftime('%b %Y') if isinstance(p['date'], date) else p['date_str']
            for p in numeric
        ]
        values = [p['value'] for p in numeric]
        unit   = numeric[-1]['unit'] or ''

        # Reuse existing trend computation
        trend_summary = self._compute_trend([
            (p['date'], p['value'], p['unit']) for p in numeric
        ])

        # Parse direction and slope from the summary string
        direction      = 'INCREASING' if 'INCREASING' in trend_summary else (
                         'DECREASING' if 'DECREASING' in trend_summary else 'STABLE')
        first_v, last_v = values[0], values[-1]
        pct_change = ((last_v - first_v) / first_v * 100) if first_v else 0.0

        slope_match = re.search(r'slope:\s*[+\-]?(\d+\.?\d*)', trend_summary)
        slope_per_month = float(slope_match.group(1)) if slope_match else 0.0
        if 'DECREASING' in trend_summary and slope_per_month > 0:
            slope_per_month = -slope_per_month

        delta_match = re.search(r'Δ\(D,t,s\)\s*=\s*([+\-]?\d+\.?\d*)', trend_summary)
        delta_score = float(delta_match.group(1)) if delta_match else 0.0

        # Unit-aware reference bands (Finnish labs). Pick the range that
        # matches the unit actually found in the patient's data so the chart
        # Y-axis stays on the same scale as the plotted values.
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

    # ── Unit-aware reference band lookup ──────────────────────────────────────

    def _get_ref_band(self, biomarker: str, unit: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Return (low, high) clinical reference limits in the same unit as the data.
        Uses the unit string extracted from the patient's record to select the
        correct scale (e.g. mg/dL vs µmol/L for creatinine).
        Returns (None, None) when no band is defined.
        """
        u = unit.lower()

        if biomarker == 'creatinine':
            if 'umol' in u or 'µmol' in u or 'μmol' in u:
                return (44, 115)      # µmol/L  (Finnish lab reference)
            return (0.5, 1.2)         # mg/dL   (default when unit unrecognised)

        if biomarker == 'hba1c':
            if '%' in u or 'percent' in u or 'ngsp' in u:
                return (None, 6.5)    # %  NGSP
            return (None, 48)         # mmol/mol  IFCC

        if biomarker == 'egfr':
            return (60, None)         # mL/min/1.73 m²  (always same unit)

        if biomarker == 'glucose':
            if 'mg' in u:
                return (70, 110)      # mg/dL
            return (3.9, 6.1)         # mmol/L

        if biomarker == 'cholesterol':
            if 'mg' in u:
                return (None, 200)    # mg/dL
            return (None, 5.0)        # mmol/L

        if biomarker == 'tsh':
            return (0.4, 4.0)         # mIU/L

        if biomarker == 'vitamin_d':
            if 'ng' in u:
                return (20, 50)       # ng/mL
            return (50, 125)          # nmol/L

        if biomarker == 'hemoglobin':
            if 'g/dl' in u or 'gdl' in u:
                return (11.7, 17.5)   # g/dL
            return (117, 175)         # g/L

        if biomarker == 'blood_pressure':
            return (None, 140)        # mmHg systolic

        return (None, None)

    # ── Fallback: no specific biomarker found ──────────────────────────────────

    def _general_temporal_context(self, patient) -> Tuple[str, List[Dict]]:
        """
        When no biomarker keyword is detected, return the most recent records
        sorted chronologically so the LLM can still reason about time.
        """
        from rag_assistant.models import MedicalChunk

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
        # Take the 8 most-recent unique documents
        seen: set = set()
        selected  = []
        for item in reversed(items):
            doc_id = str(item['chunk'].document.id)
            if doc_id not in seen:
                seen.add(doc_id)
                selected.append(item)
            if len(selected) == 8:
                break
        selected.reverse()  # back to ascending order

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

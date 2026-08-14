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

from apps.rag_assistant.services.biomarkers import BIOMARKERS, detect as _detect
from apps.rag_assistant.services.text_match import matches as _text_matches


def _biomarker_alias_match(alias: str, q: str) -> bool:
    """Retained for callers; delegates to the shared matcher."""
    return _text_matches(alias, q)

logger = logging.getLogger(__name__)


_CHART_KEYWORDS = [
    'diagram', 'chart', 'graph', 'plot', 'visuali', 'visualise',
    'show me a', 'show my', 'draw', 'display my data', 'show a',
]

# Biomarker vocabulary lives in services/biomarkers.py — the single source of
# truth shared with the query classifier. Re-exported under the original name
# so the rest of this module (and its callers) are unaffected.
_BIOMARKERS = BIOMARKERS

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
        """Canonical biomarker named in *query*. Shared with the classifier."""
        return _detect(query)

    def get_trajectory_context(
        self,
        patient,
        query: str,
        temporal_mode: Optional[str] = None,
    ) -> Tuple[str, List[Dict]]:
        """
        Build chronological context for a temporal question.

        `temporal_mode` says WHICH temporal question was asked:
          'latest'   — one value: the most recent
          'previous' — one value: the second most recent
          'trend'    — the whole series (default, and what this always did)

        The full series is included in every mode, because a latest-value answer
        still benefits from the surrounding history; what changes is which point
        is called out at the top, so the model is not left to infer "most recent"
        from an ordered list it may read in either direction.
        """
        biomarker = self.detect_biomarker(query)
        if not biomarker:
            return self._general_temporal_context(patient)

        aliases = _BIOMARKERS[biomarker]
        trajectory_points, source_chunks = self._load_trajectory(patient, aliases, biomarker)

        if not trajectory_points:
            return '', []

        context = self._format_trajectory(biomarker, trajectory_points, temporal_mode)

        # Surface genuine contradictions (same analyte, same date, different
        # values) alongside the series. Progression is already visible in the
        # trajectory itself and is deliberately not flagged.
        try:
            from apps.rag_assistant.services.conflict_service import (
                analyze_lab_values, format_conflict_notice)
            notice = format_conflict_notice(analyze_lab_values(patient, parameter=biomarker))
            if notice:
                context = f'{context}\n\n{notice}'
        except Exception as exc:
            logger.warning('conflict analysis skipped (%s)', type(exc).__name__)

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

            # The reading that currently stands: a correction supersedes what
            # was extracted, and a trend drawn from superseded numbers is a
            # trend of what the parser misread rather than of the patient.
            row = row.effective()

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
            # This path starts from ParsedLabValue, which has no link to the RAG
            # document — so the citation fields have to be resolved separately
            # or the answer ships with no sources at all.
            self._attach_document_metadata(patient, trajectory_points)
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


    # ── Citation metadata for the ORM path ────────────────────────────────────

    def _attach_document_metadata(self, patient, trajectory_points: List[Dict]) -> None:
        """
        Fill in document_id (and, when unambiguous, chunk offsets) on points
        built from ParsedLabValue rows.

        Why this is needed: the ORM trajectory path walks
        ParsedLabValue -> MedicalRecord, which never touches MedicalDocument.
        _build_sources() keys every citation off `document_id` and silently drops
        any chunk without one, so before this the entire latest/previous/trend
        family answered correctly with an empty sources panel.

        Nothing is invented. A record with no indexed document simply keeps no
        document_id, and _build_sources() then omits it — an absent citation is
        honest, a fabricated one is not.

        Both lookups are filtered by `patient`, not only by record id, so this
        cannot widen the scope of what a user can be shown even if a record id
        were somehow mismatched.
        """
        from apps.rag_assistant.models import MedicalChunk, MedicalDocument

        record_ids = {
            p['metadata'].get('record_id') for p in trajectory_points
            if p['metadata'].get('record_id')
        }
        if not record_ids:
            return

        documents = {}
        for doc in (MedicalDocument.objects
                    .filter(patient=patient, record_id__in=record_ids)
                    .order_by('record_id', '-created_at')):
            # First document wins; process_record replaces documents rather than
            # accumulating them, so in practice there is exactly one per record.
            documents.setdefault(str(doc.record_id), doc)
        if not documents:
            return

        chunks_by_document: Dict[str, List] = {}
        for chunk in MedicalChunk.objects.filter(
            patient=patient, document_id__in=[d.id for d in documents.values()]
        ):
            chunks_by_document.setdefault(str(chunk.document_id), []).append(chunk)

        for point in trajectory_points:
            meta = point['metadata']
            doc = documents.get(meta.get('record_id'))
            if doc is None:
                continue

            meta['document_id'] = str(doc.id)
            meta.setdefault('document_title', doc.title)
            meta.setdefault('document_type', doc.document_type)

            offsets = self._locate_offsets(
                chunks_by_document.get(str(doc.id), []), meta.get('parameter_name'))
            if offsets:
                meta['start_offset'], meta['end_offset'] = offsets

    @staticmethod
    def _locate_offsets(chunks: List, parameter_name):
        """
        Character range of the chunk this analyte came from, or None.

        Only returned when EXACTLY ONE chunk in the document mentions the
        analyte. Chunk windows overlap, so a parameter near a boundary appears in
        two chunks and there is no basis for choosing between them — pointing at
        the wrong passage is worse than pointing at none.
        """
        if not parameter_name or not chunks:
            return None

        needle = str(parameter_name).lower()
        matches = [c for c in chunks if needle in (c.content or '').lower()]
        if len(matches) != 1:
            return None

        meta = matches[0].metadata or {}
        start, end = meta.get('start_offset'), meta.get('end_offset')
        if isinstance(start, int) and isinstance(end, int):
            return start, end
        return None

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

    def _format_trajectory(self, biomarker: str, points: List[Dict],
                           temporal_mode: Optional[str] = None) -> str:
        display = biomarker.replace('_', ' ').title()
        lines = [
            f"=== TEMPORAL TRAJECTORY: {display.upper()} ===",
            "Ordered chronologically (oldest → newest).",
            "",
        ]

        # State the answer to a point-in-time question explicitly rather than
        # leaving the model to pick the right end of an ordered list.
        answer_line = self._point_in_time_line(display, points, temporal_mode)
        if answer_line:
            lines.append(answer_line)
            lines.append("")

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

    def _point_in_time_line(self, display: str, points: List[Dict],
                            temporal_mode: Optional[str]) -> str:
        """
        One line naming the value the question actually asked for.

        Returns '' for trend questions and whenever the requested point does not
        exist — asking for the previous value when only one reading is on file
        has no answer, and inventing one would be worse than saying nothing.
        """
        if temporal_mode not in ('latest', 'previous'):
            return ''
        if not points:
            return ''

        index = -1 if temporal_mode == 'latest' else -2
        if len(points) < abs(index):
            if temporal_mode == 'previous':
                return ('ANSWER TO THE QUESTION ASKED: only one reading is on file, '
                        'so there is no previous value.')
            return ''

        pt = points[index]
        date_label = (pt['date'].strftime('%d %B %Y')
                      if isinstance(pt['date'], date) else pt['date_str'])
        if pt['value'] is None:
            return ''
        value = f"{pt['value']} {pt['unit']}".strip()
        flag = ' [ABNORMAL]' if pt.get('is_abnormal') else ''
        label = 'MOST RECENT' if temporal_mode == 'latest' else 'PREVIOUS (second most recent)'
        return (f"ANSWER TO THE QUESTION ASKED — {label} {display}: "
                f"{value}{flag}, recorded {date_label}.")

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

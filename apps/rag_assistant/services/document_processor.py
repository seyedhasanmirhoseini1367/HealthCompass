# rag_assistant/services/document_processor.py
"""
Medical-aware document processor.

Converts a MedicalRecord into one or more MedicalDocument objects, then
chunks them into MedicalChunk objects ready for embedding.

Chunk types:
  lab_result  — one chunk per batch of lab values, plus a summary chunk
  medication  — one chunk per prescription/medication record
  wearable    — statistical summary chunk (mean/min/max per metric)
  note        — fixed-size word-overlap chunks from raw_text / discharge / imaging
  raw_text    — fallback for any other record type
"""
import logging
import math
from datetime import date
import re
from typing import List, Dict, Any

from django.conf import settings

logger = logging.getLogger(__name__)


class DocumentProcessor:

    def __init__(self):
        self.cfg          = settings.RAG_CONFIG
        self.chunk_size   = self.cfg['CHUNK_SIZE']    # words
        self.chunk_overlap = self.cfg['CHUNK_OVERLAP'] # words

    # ── Public entry point ─────────────────────────────────────────────────────

    def process_record(self, record) -> List:
        """
        Create/update MedicalDocument + MedicalChunk rows for *record*.
        Returns list of newly created MedicalChunk objects (not yet embedded).
        """
        from django.core.cache import cache

        from apps.rag_assistant.models import MedicalDocument, MedicalChunk

        # Serialise indexing per record.
        #
        # The body below deletes every document for the record and recreates it.
        # Two concurrent runs interleave as delete/delete/create/create and leave
        # TWO documents and two chunk sets for one record — duplicated evidence
        # at retrieval time and double the embedding cost. Ten records in the
        # development database carry exactly that damage.
        #
        # cache.add() is atomic, so exactly one caller acquires the lock; the
        # others skip rather than queue, because they would only redo work the
        # holder is already doing. With CACHE_URL set (Redis) this holds across
        # processes; with the locmem fallback it holds within one process only,
        # which still covers the ThreadPoolExecutor that indexes on save.
        lock_key = f'rag:index:{record.pk}'
        if not cache.add(lock_key, 1, timeout=300):
            logger.info(
                'process_record: record %s is already being indexed — skipping '
                'this duplicate run', record.pk)
            return []

        try:
            # Delete stale docs for this record so we get a clean re-index
            MedicalDocument.objects.filter(record=record).delete()

            rtype = record.record_type
            chunks_created = []

            if rtype == 'lab_result':
                chunks_created.extend(self._process_lab(record))
            elif rtype == 'prescription':
                chunks_created.extend(self._process_medication(record))
            elif rtype == 'wearable':
                chunks_created.extend(self._process_wearable(record))
            else:
                # diagnosis, imaging, discharge, vaccination, other
                chunks_created.extend(self._process_text(record))

            return chunks_created
        finally:
            cache.delete(lock_key)

    # ── Lab results ────────────────────────────────────────────────────────────

    def _process_lab(self, record) -> List:
        from apps.rag_assistant.models import MedicalDocument, MedicalChunk

        lab_values = list(record.lab_values.all())
        date_str   = str(record.record_date) if record.record_date else 'unknown date'

        # Build human-readable text for all values
        lines = [f"Lab result: {record.title} — {date_str}"]
        for lv in lab_values:
            flag  = ' [ABNORMAL]' if lv.is_abnormal else ''
            crit  = ' [CRITICAL]' if lv.is_critical else ''
            ref   = f' (ref: {lv.reference_range})' if lv.reference_range else ''
            lines.append(f"  {lv.parameter_name}: {lv.value} {lv.unit}{ref}{flag}{crit}")

        # Also include any raw text
        if record.raw_text and len(record.raw_text) > 30:
            lines.append(f"\nDocument text:\n{record.raw_text[:1500]}")

        full_text = '\n'.join(lines)

        doc = MedicalDocument.objects.create(
            patient       = record.patient,
            record        = record,
            title         = record.title,
            document_type = 'lab_result',
            content       = full_text,
            source        = str(record.file) if record.file else '',
            metadata      = {'record_date': date_str, 'record_type': record.record_type},
        )

        # Split into chunks of ~chunk_size words with overlap
        return self._make_chunks(doc, full_text, 'lab_result',
                                 {'record_date': date_str, 'record_type': 'lab_result'})

    # ── Prescriptions / medications ────────────────────────────────────────────

    def _process_medication(self, record) -> List:
        from apps.rag_assistant.models import MedicalDocument, MedicalChunk

        date_str = str(record.record_date) if record.record_date else 'unknown date'
        lines    = [f"Prescription / Medication: {record.title} — {date_str}"]

        if record.parsed_data and isinstance(record.parsed_data, dict):
            structured = record.parsed_data.get('structured', {}) or {}
            for med in structured.get('medications', []):
                name  = med.get('name', '')
                dose  = med.get('dose', '')
                freq  = med.get('frequency', '')
                route = med.get('route', '')
                lines.append(f"  Medication: {name}  Dose: {dose}  Frequency: {freq}  Route: {route}")
            if structured.get('notes'):
                lines.append(f"Notes: {structured['notes']}")

        if record.raw_text and len(record.raw_text) > 30:
            lines.append(f"\nDocument text:\n{record.raw_text[:1500]}")
        if record.notes:
            lines.append(f"Additional notes: {record.notes}")

        full_text = '\n'.join(lines)

        doc = MedicalDocument.objects.create(
            patient       = record.patient,
            record        = record,
            title         = record.title,
            document_type = 'medication',
            content       = full_text,
            source        = str(record.file) if record.file else '',
            metadata      = {'record_date': date_str, 'record_type': record.record_type},
        )

        return self._make_chunks(doc, full_text, 'medication',
                                 {'record_date': date_str, 'record_type': 'medication'})

    # ── Wearable data ──────────────────────────────────────────────────────────

    def _process_wearable(self, record) -> List:
        from apps.rag_assistant.models import MedicalDocument

        date_str = str(record.record_date) if record.record_date else 'unknown date'
        points   = list(record.wearable_points.all())

        lines = [f"Wearable data: {record.title} — {date_str}"]

        if points:
            # Group by metric, compute stats
            from collections import defaultdict
            groups: Dict[str, List[float]] = defaultdict(list)
            for p in points:
                groups[p.get_metric_display()].append(p.value)

            for metric, vals in groups.items():
                mn   = min(vals)
                mx   = max(vals)
                avg  = sum(vals) / len(vals)
                lines.append(
                    f"  {metric}: avg={avg:.1f}, min={mn:.1f}, max={mx:.1f} "
                    f"(n={len(vals)} readings)"
                )
        else:
            if record.raw_text:
                lines.append(f"\nDocument text:\n{record.raw_text[:1500]}")

        if record.notes:
            lines.append(f"Notes: {record.notes}")

        full_text = '\n'.join(lines)

        doc = MedicalDocument.objects.create(
            patient       = record.patient,
            record        = record,
            title         = record.title,
            document_type = 'wearable',
            content       = full_text,
            source        = str(record.file) if record.file else '',
            metadata      = {'record_date': date_str, 'record_type': record.record_type},
        )

        # Wearable summaries are usually short — one chunk is fine
        return self._make_chunks(doc, full_text, 'wearable',
                                 {'record_date': date_str, 'record_type': 'wearable'})

    # ── Generic text (notes, discharge, imaging, etc.) ─────────────────────────

    def _process_text(self, record) -> List:
        from apps.rag_assistant.models import MedicalDocument

        date_str  = str(record.record_date) if record.record_date else 'unknown date'
        rtype_lbl = record.get_record_type_display()

        lines = [f"{rtype_lbl}: {record.title} — {date_str}"]

        # Parsed structured data
        if record.parsed_data and isinstance(record.parsed_data, dict):
            structured = record.parsed_data.get('structured', {}) or {}
            if structured.get('summary'):
                lines.append(f"Summary: {structured['summary']}")
            for dx in structured.get('diagnoses', []):
                lines.append(f"Diagnosis: {dx.get('description','')} {dx.get('code','')}")
            for med in structured.get('medications', []):
                lines.append(
                    f"Medication: {med.get('name','')} {med.get('dose','')} "
                    f"{med.get('frequency','')}"
                )

        if record.raw_text:
            lines.append(f"\nDocument text:\n{record.raw_text}")

        if record.notes:
            lines.append(f"\nNotes: {record.notes}")

        full_text = '\n'.join(lines)

        # Map record_type → document_type
        dtype_map = {
            'diagnosis':  'note',
            'imaging':    'note',
            'discharge':  'note',
            'vaccination': 'note',
        }
        dtype = dtype_map.get(record.record_type, 'raw_text')

        doc = MedicalDocument.objects.create(
            patient       = record.patient,
            record        = record,
            title         = record.title,
            document_type = dtype,
            content       = full_text,
            source        = str(record.file) if record.file else '',
            metadata      = {'record_date': date_str, 'record_type': record.record_type},
        )

        return self._make_chunks(doc, full_text, dtype,
                                 {'record_date': date_str, 'record_type': record.record_type})

    # ── Chunking helpers ───────────────────────────────────────────────────────

    def _make_chunks(self, doc, text: str, doc_type: str, extra_meta: dict) -> List:
        """
        Split *text* into overlapping word-window chunks and persist them.

        Every chunk after the first gets a one-line context header naming the
        document and its date. Retrieval returns chunks, not documents, so
        without this a chunk reading "Glucose: 105 mg/dL" arrives at the model
        with no date at all — the date lived in the first chunk's header line and
        nowhere else. That is precisely how a stale value gets reported as
        current. The header is one short line, not a copy of the metadata.
        """
        from apps.rag_assistant.models import MedicalChunk

        windows = self._split_words_with_offsets(text)
        header  = self._context_header(doc, extra_meta)
        chunk_objs = []
        for idx, (window_text, start_offset, end_offset) in enumerate(windows):
            # Character range within MedicalDocument.content. Exact, and enough
            # for a citation to point at the passage rather than only the
            # document. Page and section are deliberately absent — see
            # _split_words_with_offsets.
            meta = {
                **extra_meta,
                'doc_type':     doc_type,
                'start_offset': start_offset,
                'end_offset':   end_offset,
            }
            # The first window already opens with the document's own title/date
            # line, so prefixing it would duplicate that line verbatim.
            content = window_text if idx == 0 else f'{header}\n{window_text}'
            chunk_objs.append(MedicalChunk(
                document    = doc,
                patient     = doc.patient,
                content     = content,
                chunk_index = idx,
                metadata    = meta,
            ))

        MedicalChunk.objects.bulk_create(chunk_objs)
        return chunk_objs

    @staticmethod
    def _context_header(doc, extra_meta: dict) -> str:
        """
        Minimal provenance line: what document this is and when it is from.

        Deliberately short — it is prepended to every continuation chunk and is
        also embedded, so anything longer would dilute the chunk's own content
        in the vector.
        """
        date_str = extra_meta.get('record_date') or 'unknown date'
        title    = (doc.title or 'Record').strip()
        return f'[{title} — {date_str}] (continued)'

    def _split_words(self, text: str) -> List[str]:
        """Backward-compatible wrapper: windows only, no offsets."""
        return [w for w, _start, _end in self._split_words_with_offsets(text)]

    def _split_words_with_offsets(self, text: str):
        """
        Split into overlapping word windows, returning (text, start, end) where
        start/end are character offsets into *text*.

        Offsets are computed from the real token positions rather than by
        re-measuring the joined window, so they stay exact even where the source
        had irregular whitespace — which OCR output routinely does.

        Page numbers are NOT recorded: the PDF parser joins pages into a single
        string before this point, so page boundaries no longer exist here.
        Recording a guessed page would be worse than recording none, since a
        citation that points at the wrong page is harder to catch than one that
        admits it does not know. Preserving page offsets would mean changing the
        parser, which is a separate change.
        """
        tokens = [(m.group(0), m.start(), m.end()) for m in re.finditer(r'\S+', text)]
        if not tokens:
            snippet = text[:2000]
            return [(snippet, 0, len(snippet))]

        size    = self.chunk_size
        step    = max(1, size - self.chunk_overlap)
        windows = []

        for start in range(0, len(tokens), step):
            end    = self._safe_boundary(tokens, start, min(start + size, len(tokens)))
            window = tokens[start:end]
            if window:
                windows.append((
                    ' '.join(t[0] for t in window),
                    window[0][1],
                    window[-1][2],
                ))
            if start + size >= len(tokens):
                break

        return windows

    #: A token ending in ':' is a label whose value is the token(s) after it —
    #: "Glucose:" then "105 mg/dL". Splitting between them leaves one chunk with
    #: a name and no number and the next with a number and no name, and a number
    #: without its analyte is worse than useless in a medical context.
    _MAX_BOUNDARY_SHIFT = 6

    def _safe_boundary(self, tokens, start: int, end: int) -> int:
        """
        Nudge a window's end so it does not cut a key away from its value.

        Only ever pulls the boundary BACK, never forward, so a window can shrink
        but never exceed chunk_size. Gives up after _MAX_BOUNDARY_SHIFT tokens
        rather than distorting the window — overlap already means the pair
        appears intact in the following chunk.
        """
        if end >= len(tokens) or end <= start + 1:
            return end

        limit = max(start + 1, end - self._MAX_BOUNDARY_SHIFT)
        cursor = end
        while cursor > limit and self._is_dangling_label(tokens, cursor):
            cursor -= 1
        return cursor if cursor > start else end

    @staticmethod
    def _is_dangling_label(tokens, end: int) -> bool:
        """True when the window would end on a label, orphaning its value."""
        last = tokens[end - 1][0]
        if last.endswith(':'):
            return True
        # "Glucose : 105" — a bare colon separator left at the edge.
        return last == ':'

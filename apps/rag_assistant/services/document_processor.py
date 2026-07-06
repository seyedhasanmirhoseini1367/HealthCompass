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
        from apps.rag_assistant.models import MedicalDocument, MedicalChunk

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
        """Split *text* into overlapping word-window chunks and persist them."""
        from apps.rag_assistant.models import MedicalChunk

        windows = self._split_words(text)
        chunk_objs = []
        for idx, window_text in enumerate(windows):
            meta = {**extra_meta, 'doc_type': doc_type}
            chunk_objs.append(MedicalChunk(
                document    = doc,
                patient     = doc.patient,
                content     = window_text,
                chunk_index = idx,
                metadata    = meta,
            ))

        MedicalChunk.objects.bulk_create(chunk_objs)
        return chunk_objs

    def _split_words(self, text: str) -> List[str]:
        """
        Split text into overlapping windows of self.chunk_size words,
        with self.chunk_overlap word overlap between consecutive windows.
        """
        words  = text.split()
        size   = self.chunk_size
        step   = max(1, size - self.chunk_overlap)
        chunks = []

        for start in range(0, len(words), step):
            window = words[start: start + size]
            if window:
                chunks.append(' '.join(window))
            if start + size >= len(words):
                break

        return chunks if chunks else [text[:2000]]

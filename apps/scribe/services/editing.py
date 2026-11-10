"""
Clinician edits to a generated note.

Grounding is the point of this app, so an edit never silently keeps a claim
of support it may have lost:

* Unchanged items keep their citations and evidence as they were.
* A changed item keeps its citations but is flagged "edited": the UI says the
  text no longer necessarily matches what those segments say.
* An added item has no citations and is flagged "added".
* A removed item drops out together with its evidence, so the evidence lists
  stay aligned with the items by position.

The model's original reply stays in Note.raw_output.
"""
from __future__ import annotations

from django.utils import timezone

from .evidence import EVIDENCE_SECTIONS
from .note_generation import SECTIONS

MAX_ITEM_CHARS = 2000
MAX_NEW_ITEMS = 30


def apply_edits(note, data) -> bool:
    """Apply a submitted edit form (a QueryDict-like `data`). Returns True if anything changed."""
    new_soap, new_evidence, changed = {}, {}, False
    for section in SECTIONS:
        items = note.soap.get(section, []) or []
        evidence = (note.evidence.get(section) or []) if section in EVIDENCE_SECTIONS else []
        kept, kept_ev = [], []
        for pos, item in enumerate(items):
            if data.get(f'{section}-{pos}-delete'):
                changed = True
                continue
            text = (data.get(f'{section}-{pos}-text', item['text']) or '').strip()[:MAX_ITEM_CHARS]
            if not text:
                changed = True
                continue
            new_item = dict(item)
            if text != item['text']:
                new_item['text'] = text
                new_item['edited'] = True
                changed = True
            kept.append(new_item)
            if section in EVIDENCE_SECTIONS:
                kept_ev.append(evidence[pos] if pos < len(evidence) else [])
        added = [line.strip()[:MAX_ITEM_CHARS]
                 for line in (data.get(f'{section}-new', '') or '').splitlines() if line.strip()]
        for text in added[:MAX_NEW_ITEMS]:
            kept.append({'text': text, 'source_segments': [], 'added': True})
            if section in EVIDENCE_SECTIONS:
                kept_ev.append([])
            changed = True
        new_soap[section] = kept
        if section in EVIDENCE_SECTIONS:
            new_evidence[section] = kept_ev

    if changed:
        note.soap = new_soap
        note.evidence = {**note.evidence, **new_evidence}
        note.edited_at = timezone.now()
        note.save(update_fields=['soap', 'evidence', 'edited_at'])
    return changed

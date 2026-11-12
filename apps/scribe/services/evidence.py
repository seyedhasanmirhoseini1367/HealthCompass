"""
Attach supporting guideline excerpts to Assessment and Plan items.

Uses the existing curated clinical knowledge base (GeneralKnowledgeService:
Käypä hoito / Terveyskirjasto / THL) — the same retriever the assistant uses for
general questions. The patient-records retriever is deliberately not used: it is
scoped to one patient's own documents, which is not "evidence" for a note.

Evidence is best-effort. A retrieval failure leaves that item without sources;
it never fails the encounter.
"""
import logging

logger = logging.getLogger(__name__)

EVIDENCE_SECTIONS = ('assessment', 'plan')
TOP_K = 3
SNIPPET_CHARS = 280


def _retriever():
    from apps.rag_assistant.services.general_knowledge_service import GeneralKnowledgeService
    return GeneralKnowledgeService()


def find_evidence(retriever, text, top_k=TOP_K):
    try:
        hits = retriever.retrieve(text, top_k=top_k) or []
    except Exception as exc:
        logger.warning('scribe: evidence retrieval failed (%s)', type(exc).__name__)
        return []
    out = []
    for hit in hits[:top_k]:
        meta = hit.get('metadata') or {}
        out.append({
            'title':       meta.get('title', ''),
            'source_name': meta.get('source_name', ''),
            'source_url':  meta.get('source_url', ''),
            'snippet':     (hit.get('text') or '')[:SNIPPET_CHARS],
            'score':       round(float(hit.get('score') or 0.0), 3),
        })
    return out


def attach_evidence(note):
    retriever = _retriever()
    evidence = {}
    for section in EVIDENCE_SECTIONS:
        evidence[section] = [find_evidence(retriever, item['text'])
                             for item in note.soap.get(section, [])]
    note.evidence = evidence
    note.save(update_fields=['evidence'])
    return note

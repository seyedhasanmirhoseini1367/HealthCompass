# rag_assistant/services/general_knowledge_service.py
"""
General Knowledge Service — retrieves from the curated public health knowledge
base (GeneralKnowledgeChunk) instead of patient-specific records.

Mode classification:
  PERSONAL — query explicitly about the patient's own data
  GENERAL  — question about health concepts with no personal reference
  HYBRID   — personal context + request for general explanation
              → retrieves from BOTH sources and merges context

Classification is keyword-based (no LLM call) so it adds <5ms latency.
"""
import logging
import re
from collections import Counter
from typing import List, Dict, Any, Tuple

import numpy as np
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Mode classification keywords ──────────────────────────────────────────────

_PERSONAL_MARKERS = re.compile(
    r"\b(my|mine|i have|i am|am i|i've|i was|i've been|my last|my latest|"
    r"my recent|my results|my records|my levels|my history|my medications?|"
    r"my prescription|my diagnosis|for me|in my case)\b",
    re.IGNORECASE,
)

_GENERAL_QUESTION_PATTERNS = re.compile(
    r"\b(what is|what are|what does|what do|how does|how do|why is|why do|"
    r"explain|tell me about|what causes|what happens|is it dangerous|"
    r"is it serious|what are the symptoms|how is it treated|"
    r"what should i know about|what is the difference|"
    r"how common is|what medication|general(ly)?|in general|"
    r"normal range|reference range|typical|usually|normally)\b",
    re.IGNORECASE,
)


def classify_query_mode(query: str) -> str:
    """
    Return 'personal', 'general', or 'hybrid'.

    Logic:
      - No personal markers → GENERAL
      - Personal markers + general question patterns → HYBRID
      - Personal markers only → PERSONAL
    """
    has_personal = bool(_PERSONAL_MARKERS.search(query))
    has_general  = bool(_GENERAL_QUESTION_PATTERNS.search(query))

    if not has_personal:
        return 'general'
    if has_personal and has_general:
        return 'hybrid'
    return 'personal'


# ── General Knowledge Retrieval ───────────────────────────────────────────────

class GeneralKnowledgeService:
    """
    Retrieves relevant chunks from GeneralKnowledgeChunk using the same
    hybrid BM25 + cosine + MMR pipeline as RetrievalService.
    """

    def __init__(self):
        cfg = settings.RAG_CONFIG
        self.top_k         = cfg.get('GENERAL_TOP_K', cfg['TOP_K'])
        self.bm25_weight   = cfg['BM25_WEIGHT']
        self.sem_weight    = cfg['SEMANTIC_WEIGHT']
        self.mmr_lambda    = cfg['MMR_LAMBDA']
        self.sim_threshold = cfg.get('GENERAL_SIM_THRESHOLD', 0.10)

    def retrieve(self, query: str, top_k: int = None) -> List[Dict[str, Any]]:
        """
        Return up to top_k dicts: {'text', 'score', 'metadata', 'source_url'}.
        """
        from apps.rag_assistant.models import GeneralKnowledgeChunk
        from apps.rag_assistant.services.embedding_service import (
            COMPAT_OK, EmbeddingService, _log_incompatible,
            active_embedding_dim, active_embedding_model,
            classify_embedding, strict_provenance,
        )

        chunks = GeneralKnowledgeChunk.objects.exclude(embedding=None)
        if not chunks.exists():
            logger.warning('GeneralKnowledgeService: no indexed chunks in DB')
            return []

        active_model = active_embedding_model()
        active_dim   = active_embedding_dim()
        strict       = strict_provenance()

        texts, vectors, meta = [], [], []
        skipped = Counter()
        for c in chunks:
            try:
                raw = bytes(c.embedding)
                if raw[:2] in (b'\x80\x03', b'\x80\x04', b'\x80\x05'):
                    import pickle
                    vec = pickle.loads(raw)
                else:
                    vec = np.frombuffer(raw, dtype=np.float32)

                # Without this, vectors of differing length reach np.array()
                # below and produce a ragged/object array — a hard failure or,
                # worse, a silently meaningless similarity matrix.
                status = classify_embedding(
                    stored_model = getattr(c, 'embedding_model', ''),
                    stored_dim   = getattr(c, 'embedding_dimensions', None),
                    actual_dim   = len(vec),
                    active_model = active_model,
                    active_dim   = active_dim,
                    strict       = strict,
                )
                if status != COMPAT_OK:
                    skipped[status] += 1
                    continue

                texts.append(c.content)
                vectors.append(vec)
                meta.append({
                    'chunk_id':    str(c.pk),
                    'title':       c.title,
                    'source_name': c.source_name,
                    'source_url':  c.source_url,
                    'topic':       c.topic,
                    'is_general':  True,
                })
            except Exception:
                continue

        _log_incompatible('GeneralKnowledgeService.retrieve', skipped)

        if not texts:
            return []

        matrix = np.array(vectors, dtype=np.float32)
        emb    = EmbeddingService()
        q_vec  = emb.embed(query)

        bm25   = self._bm25_scores(query, texts)
        cosine = self._cosine_scores(q_vec, matrix)
        hybrid = self.bm25_weight * bm25 + self.sem_weight * cosine

        valid = np.where(hybrid >= self.sim_threshold)[0]
        if len(valid) == 0:
            return []

        k       = top_k or self.top_k
        indices = self._mmr(q_vec, matrix, hybrid, valid, k)

        return [
            {'text': texts[i], 'score': float(hybrid[i]), 'metadata': meta[i]}
            for i in indices
        ]

    # ── BM25 ─────────────────────────────────────────────────────────────────

    _STOPWORDS = frozenset([
        'a', 'an', 'the', 'is', 'it', 'in', 'on', 'at', 'to', 'for',
        'of', 'and', 'or', 'my', 'me', 'i', 'was', 'are', 'be', 'do',
    ])

    def _tokenize(self, text: str) -> List[str]:
        text = text.lower()
        text = re.sub(r'[-/]', ' ', text)
        text = re.sub(r'[^\w\s]', ' ', text)
        return [t for t in text.split() if t not in self._STOPWORDS and len(t) > 1]

    def _bm25_scores(self, query: str, texts: List[str]) -> np.ndarray:
        try:
            from rank_bm25 import BM25Okapi
            corpus = [self._tokenize(t) for t in texts]
            bm25   = BM25Okapi(corpus)
            scores = bm25.get_scores(self._tokenize(query))
            mx     = scores.max()
            return (scores / mx) if mx > 0 else scores
        except Exception:
            return np.zeros(len(texts), dtype=np.float32)

    def _cosine_scores(self, q_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        q_norm = np.linalg.norm(q_vec)
        if q_norm == 0 or matrix.shape[0] == 0:
            return np.zeros(matrix.shape[0], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        norms = np.where(norms == 0, 1e-9, norms)
        return (matrix @ q_vec) / (norms * q_norm)

    def _mmr(self, q_vec, matrix, scores, valid, k) -> List[int]:
        lam, selected, remaining = self.mmr_lambda, [], list(valid)
        while remaining and len(selected) < k:
            if not selected:
                chosen = remaining[int(np.argmax([scores[i] for i in remaining]))]
            else:
                sel_mat  = matrix[selected]
                best_mmr = -np.inf
                chosen   = remaining[0]
                for cand in remaining:
                    rel    = scores[cand]
                    c_vec  = matrix[cand]
                    c_norm = np.linalg.norm(c_vec)
                    sims   = (sel_mat @ c_vec) / (np.linalg.norm(sel_mat, axis=1) * (c_norm or 1e-9) + 1e-9)
                    score  = lam * rel - (1 - lam) * float(sims.max())
                    if score > best_mmr:
                        best_mmr, chosen = score, cand
            selected.append(chosen)
            remaining.remove(chosen)
        return selected

    # ── Indexing ──────────────────────────────────────────────────────────────

    def index_articles(self, articles: List[Dict]) -> int:
        """
        Chunk, embed and store a list of article dicts:
          {'title', 'source_name', 'source_url', 'topic', 'content'}
        Returns number of chunks created.
        """
        from apps.rag_assistant.models import GeneralKnowledgeChunk
        from apps.rag_assistant.services.embedding_service import (
            EmbeddingService, active_embedding_model,
        )

        emb     = EmbeddingService()
        created = 0

        for art in articles:
            title       = art['title']
            source_name = art['source_name']
            source_url  = art.get('source_url', '')
            topic       = art.get('topic', '')
            text        = art['content']

            chunks = self._chunk_text(text)
            for idx, chunk_text in enumerate(chunks):
                vec = emb.embed(chunk_text)

                GeneralKnowledgeChunk.objects.update_or_create(
                    title=title, chunk_index=idx,
                    defaults={
                        'source_name': source_name,
                        'source_url':  source_url,
                        'topic':       topic,
                        'content':     chunk_text,
                        'embedding':   vec.astype(np.float32).tobytes(),
                        'embedding_model':         active_embedding_model(),
                        'embedding_model_version': settings.RAG_CONFIG.get('EMBEDDING_MODEL_VERSION', ''),
                        'embedding_dimensions':    int(len(vec)),
                        'embedded_at':             timezone.now(),
                    },
                )
                created += 1

        logger.info('GeneralKnowledgeService: indexed %d chunks from %d articles', created, len(articles))
        return created

    def _chunk_text(self, text: str, size: int = 200, overlap: int = 40) -> List[str]:
        words  = text.split()
        chunks = []
        start  = 0
        while start < len(words):
            end = min(start + size, len(words))
            chunks.append(' '.join(words[start:end]))
            if end == len(words):
                break
            start = end - overlap
        return chunks

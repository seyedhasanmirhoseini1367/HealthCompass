# rag_assistant/services/retrieval_service.py
"""
Hybrid retrieval: BM25 keyword score + cosine semantic score,
with time-decay weighting and MMR diversity re-ranking.

Pipeline:
  1. load_patient_embeddings()  — texts, vectors, metadata from DB
  2. bm25_scores()              — keyword relevance
  3. cosine_scores()            — semantic similarity
  4. hybrid_scores()            — weighted blend
  5. time_decay()               — down-weight old records
  6. mmr_rerank()               — diversify top-K
"""
import logging
import math
from datetime import date, datetime
from typing import List, Dict, Any, Tuple, Optional

import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)


class RetrievalService:

    def __init__(self):
        self.cfg             = settings.RAG_CONFIG
        self.top_k           = self.cfg['TOP_K']
        self.bm25_weight     = self.cfg['BM25_WEIGHT']
        self.sem_weight      = self.cfg['SEMANTIC_WEIGHT']
        self.decay_days      = self.cfg['TIME_DECAY_DAYS']
        self.decay_factor    = self.cfg['TIME_DECAY_FACTOR']
        self.mmr_lambda      = self.cfg['MMR_LAMBDA']
        self.sim_threshold   = self.cfg['SIM_THRESHOLD']

    # ── Main entry point ───────────────────────────────────────────────────────

    def retrieve(
        self,
        patient,
        query: str,
        document_type: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return up to *top_k* dicts: {'text', 'score', 'metadata'}.
        Falls back gracefully if no embeddings exist yet.
        """
        from rag_assistant.services.embedding_service import EmbeddingService

        emb_svc = EmbeddingService()
        texts, matrix, meta = emb_svc.load_patient_embeddings(patient, document_type)

        if not texts:
            return []

        k = top_k or self.top_k

        # 1. BM25
        bm25   = self._bm25_scores(query, texts)
        # 2. Cosine similarity
        q_vec  = emb_svc.embed(query)
        cosine = self._cosine_scores(q_vec, matrix)
        # 3. Hybrid blend
        hybrid = self.bm25_weight * bm25 + self.sem_weight * cosine
        # 4. Time decay
        hybrid = self._apply_time_decay(hybrid, meta)
        # 5. Threshold filter
        valid  = np.where(hybrid >= self.sim_threshold)[0]
        if len(valid) == 0:
            valid = np.argsort(hybrid)[-k:][::-1]  # fallback: take top-k anyway

        # 6. MMR re-rank
        indices = self._mmr(q_vec, matrix, hybrid, valid, k)

        results = []
        for idx in indices:
            results.append({
                'text':     texts[idx],
                'score':    float(hybrid[idx]),
                'metadata': meta[idx],
            })
        return results

    # ── BM25 ───────────────────────────────────────────────────────────────────

    def _bm25_scores(self, query: str, texts: List[str]) -> np.ndarray:
        try:
            from rank_bm25 import BM25Okapi
            tokenized_corpus = [t.lower().split() for t in texts]
            bm25             = BM25Okapi(tokenized_corpus)
            scores           = bm25.get_scores(query.lower().split())
            mx               = scores.max()
            return (scores / mx) if mx > 0 else scores
        except ImportError:
            logger.warning('rank-bm25 not installed — BM25 disabled')
            return np.zeros(len(texts), dtype=np.float32)
        except Exception as e:
            logger.warning('BM25 failed: %s', e)
            return np.zeros(len(texts), dtype=np.float32)

    # ── Cosine similarity ──────────────────────────────────────────────────────

    def _cosine_scores(self, q_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        """Vectorised cosine similarity between query and all chunk embeddings."""
        if matrix.shape[0] == 0:
            return np.zeros(0, dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm == 0:
            return np.zeros(matrix.shape[0], dtype=np.float32)
        norms  = np.linalg.norm(matrix, axis=1)
        norms  = np.where(norms == 0, 1e-9, norms)
        return (matrix @ q_vec) / (norms * q_norm)

    # ── Time decay ─────────────────────────────────────────────────────────────

    def _apply_time_decay(self, scores: np.ndarray, meta: List[Dict]) -> np.ndarray:
        scores = scores.copy()
        today  = date.today()
        for i, m in enumerate(meta):
            rd = m.get('record_date')
            if not rd:
                continue
            try:
                if isinstance(rd, str):
                    rd = date.fromisoformat(rd)
                age_days = (today - rd).days
                if age_days > self.decay_days:
                    t = min(1.0, (age_days - self.decay_days) / self.decay_days)
                    scores[i] *= (1 - self.decay_factor * t)
            except Exception:
                pass
        return scores

    # ── MMR re-ranking ─────────────────────────────────────────────────────────

    def _mmr(
        self,
        q_vec:   np.ndarray,
        matrix:  np.ndarray,
        scores:  np.ndarray,
        valid:   np.ndarray,
        k:       int,
    ) -> List[int]:
        """
        Maximal Marginal Relevance selection from *valid* indices.
        mmr_lambda=1 → pure relevance, 0 → pure diversity.
        """
        lam      = self.mmr_lambda
        selected = []
        remaining = list(valid)

        # Pre-compute cosine similarity between candidates
        sub    = matrix[remaining]
        q_norm = np.linalg.norm(q_vec)

        while remaining and len(selected) < k:
            if not selected:
                # Pick highest-scoring candidate first
                best_local = int(np.argmax([scores[i] for i in remaining]))
                chosen     = remaining[best_local]
            else:
                # For each remaining candidate, compute max similarity to selected
                sel_mat  = matrix[selected]
                best_mmr = -np.inf
                chosen   = remaining[0]
                for cand in remaining:
                    rel    = scores[cand]
                    c_vec  = matrix[cand]
                    c_norm = np.linalg.norm(c_vec)
                    if c_norm == 0:
                        max_sim = 0.0
                    else:
                        sims    = (sel_mat @ c_vec) / (
                            np.linalg.norm(sel_mat, axis=1) * c_norm + 1e-9
                        )
                        max_sim = float(sims.max())
                    mmr_score = lam * rel - (1 - lam) * max_sim
                    if mmr_score > best_mmr:
                        best_mmr = mmr_score
                        chosen   = cand

            selected.append(chosen)
            remaining.remove(chosen)

        return selected

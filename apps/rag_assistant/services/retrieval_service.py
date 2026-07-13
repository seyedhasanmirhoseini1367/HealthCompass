# rag_assistant/services/retrieval_service.py
"""
Hybrid retrieval: BM25 keyword score + cosine semantic score,
with time-decay weighting, adaptive query-intent weights, context-type boost,
and MMR diversity re-ranking.

Pipeline:
  1. load_patient_embeddings()  — texts, vectors, metadata from DB
  2. classify_query_intent()    — 'temporal'|'medication'|'diagnostic'|'general'
  3. bm25_scores()              — keyword relevance
  4. cosine_scores()            — semantic similarity
  5. hybrid_scores()            — intent-adaptive weighted blend  ← PhD proposal α·BM25 + β·cos
  6. time_decay()               — down-weight old records
  7. context_type_boost()       — boost chunks whose doc-type aligns with intent ← PhD proposal δ·Context
  8. threshold filter           — drop anything below SIM_THRESHOLD
  9. mmr_rerank()               — diversify top-K

Adaptive weights (PhD proposal Score formula):
    Score(q,D) = α·BM25 + β·cos + γ·Δ(D,t,s) + δ·Context(D,i)
    γ·Δ is handled by time_decay(); δ·Context is the context_type_boost().
    α and β are selected per query intent:
        temporal   → α=0.20, β=0.80  (semantic focus for trend reasoning)
        medication → α=0.50, β=0.50  (exact term match for drug names)
        diagnostic → α=0.30, β=0.70  (concept-heavy diagnostic terms)
        general    → α=0.35, β=0.65  (default)
"""
import logging
import math
from datetime import date, datetime
from typing import List, Dict, Any, Optional, Tuple

import numpy as np
from django.conf import settings

logger = logging.getLogger(__name__)


# ── Query intent → document_type alignment map ────────────────────────────────
# Used by _apply_context_boost() to implement δ·Context(D,i).
_INTENT_DOCTYPE_MAP: Dict[str, List[str]] = {
    'lab_results': ['lab_result'],
    'medication':  ['medication'],
    'diagnostic':  ['note', 'raw_text'],
    'wearable':    ['wearable'],
    'temporal':    ['lab_result', 'wearable'],   # trajectory queries benefit from all numeric records
    'general':     [],                            # no boost for general
}

# Intent keyword map for classify_query_intent()
_INTENT_KEYWORDS: Dict[str, List[str]] = {
    'temporal': [
        'getting better', 'getting worse', 'over time', 'trend', 'trending',
        'improving', 'worsening', 'trajectory', 'progression', 'changed',
        'going up', 'going down', 'increasing', 'decreasing', 'over the past',
        'last year', 'last month', 'last 6 months', 'history of my',
    ],
    'medication': [
        'medication', 'medicine', 'drug', 'prescription', 'dose', 'dosage',
        'tablet', 'capsule', 'pill', 'mg', 'side effect', 'interaction',
        'metformin', 'insulin', 'statin', 'taking', 'prescribed', 'pharmacy',
    ],
    'diagnostic': [
        'diagnosis', 'condition', 'disease', 'symptom', 'imaging', 'mri',
        'ct scan', 'x-ray', 'xray', 'ultrasound', 'discharge', 'finding',
        'impression', 'clinical note', 'assessment', 'report',
    ],
    'lab_results': [
        'lab', 'blood test', 'result', 'cholesterol', 'glucose', 'hba1c',
        'creatinine', 'thyroid', 'tsh', 'wbc', 'platelet', 'vitamin',
        'egfr', 'gfr', 'kidney function', 'abnormal', 'reference range',
    ],
}


class RetrievalService:

    def __init__(self):
        self.cfg           = settings.RAG_CONFIG
        self.top_k         = self.cfg['TOP_K']
        self.bm25_weight   = self.cfg['BM25_WEIGHT']
        self.sem_weight    = self.cfg['SEMANTIC_WEIGHT']
        self.decay_days    = self.cfg['TIME_DECAY_DAYS']
        self.decay_factor  = self.cfg['TIME_DECAY_FACTOR']
        self.mmr_lambda    = self.cfg['MMR_LAMBDA']
        self.sim_threshold = self.cfg['SIM_THRESHOLD']
        self.intent_weights = self.cfg.get('INTENT_WEIGHTS', {
            'temporal':   (0.20, 0.80),
            'medication': (0.50, 0.50),
            'diagnostic': (0.30, 0.70),
            'general':    (0.35, 0.65),
        })
        self.ctx_boost            = self.cfg.get('CONTEXT_TYPE_BOOST', 0.08)
        # Stage-1 widens retrieval by this factor before Stage-2 LLM reranking
        self.rerank_recall_factor = self.cfg.get('RERANK_RECALL_FACTOR', 5)

    # ── Main entry point ───────────────────────────────────────────────────────

    def retrieve(
        self,
        patient,
        query:         str,
        document_type: Optional[str]  = None,
        top_k:         Optional[int]  = None,
        query_intent:  Optional[str]  = None,
    ) -> List[Dict[str, Any]]:
        """
        Two-stage retrieval:
          Stage 1 — hybrid BM25 + cosine + time decay + context boost + MMR
                    over a widened recall set (top_k × RERANK_RECALL_FACTOR).
          Stage 2 — LLM reranker (Groq) narrows to top_k with cross-attention
                    precision. Falls back to Stage-1 order if Groq is unavailable.
        """
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        emb_svc = EmbeddingService()
        texts, matrix, meta = emb_svc.load_patient_embeddings(patient, document_type)

        if not texts:
            return []

        k = top_k or self.top_k

        intent = query_intent or self.classify_query_intent(query)
        bm25_w, sem_w = self.intent_weights.get(intent, (self.bm25_weight, self.sem_weight))
        logger.debug('retrieve: intent=%s  α(BM25)=%.2f  β(sem)=%.2f', intent, bm25_w, sem_w)

        # ── Stage 1: widened recall ────────────────────────────────────────────
        recall_k = min(k * self.rerank_recall_factor, len(texts))

        bm25   = self._bm25_scores(query, texts)
        q_vec  = emb_svc.embed(query)
        cosine = self._cosine_scores(q_vec, matrix)
        hybrid = bm25_w * bm25 + sem_w * cosine
        hybrid = self._apply_time_decay(hybrid, meta)
        hybrid = self._apply_context_boost(hybrid, meta, intent)

        valid = np.where(hybrid >= self.sim_threshold)[0]
        if len(valid) == 0:
            return []

        recall_indices = self._mmr(q_vec, matrix, hybrid, valid, recall_k)
        candidates = [
            {'text': texts[i], 'score': float(hybrid[i]), 'metadata': meta[i]}
            for i in recall_indices
        ]

        if len(candidates) <= k:
            return candidates

        # ── Stage 2: LLM reranker ──────────────────────────────────────────────
        reranked = self._llm_rerank(query, candidates, top_k=k)
        return reranked

    def _llm_rerank(
        self,
        query:      str,
        candidates: List[Dict[str, Any]],
        top_k:      int,
    ) -> List[Dict[str, Any]]:
        """
        Ask Groq to order the candidate chunks by relevance to *query*.
        Returns up to top_k chunks in ranked order.
        Falls back to the original Stage-1 order on any error.
        """
        from django.conf import settings
        api_key = getattr(settings, 'GROQ_API_KEY', '')
        if not api_key:
            return candidates[:top_k]

        try:
            from groq import Groq
            client = Groq(api_key=api_key)

            # Build numbered list — truncate each chunk to 300 chars to stay within tokens
            chunk_lines = "\n".join(
                f"[{i+1}] {c['text'][:300].strip()}"
                for i, c in enumerate(candidates)
            )
            prompt = (
                f"Query: {query}\n\n"
                f"Rank the following {len(candidates)} medical record excerpts from most to least "
                f"relevant to the query. Return ONLY a JSON array of 1-based indices, e.g. [3,1,5,2,4]. "
                f"No explanation, no markdown.\n\n"
                f"{chunk_lines}"
            )

            resp = client.chat.completions.create(
                model='llama-3.1-8b-instant',
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0,
                max_tokens=64,
            )
            raw = resp.choices[0].message.content.strip()

            import json, re as _re
            # Strip accidental markdown
            raw = _re.sub(r'^```[^\n]*\n?|\n?```$', '', raw).strip()
            order = json.loads(raw)   # e.g. [3, 1, 5, 2, 4]

            reranked = []
            seen = set()
            for rank_1based in order:
                idx = int(rank_1based) - 1
                if 0 <= idx < len(candidates) and idx not in seen:
                    seen.add(idx)
                    reranked.append(candidates[idx])
                if len(reranked) == top_k:
                    break

            # Append any candidates the LLM omitted (safety net)
            for i, c in enumerate(candidates):
                if i not in seen and len(reranked) < top_k:
                    reranked.append(c)

            logger.debug('LLM reranker: %d → %d chunks', len(candidates), len(reranked))
            return reranked

        except Exception as exc:
            logger.warning('LLM reranker failed (%s), using Stage-1 order', exc)
            return candidates[:top_k]

    # ── Query intent classifier ────────────────────────────────────────────────

    def classify_query_intent(self, query: str) -> str:
        """
        Return the dominant intent of *query*: temporal | medication |
        diagnostic | lab_results | general.

        The order of checking matters: temporal is checked first because
        temporal queries often also mention lab keywords.
        """
        q = query.lower()
        for intent in ('temporal', 'medication', 'diagnostic', 'lab_results'):
            keywords = _INTENT_KEYWORDS.get(intent, [])
            if any(kw in q for kw in keywords):
                return intent
        return 'general'

    # ── BM25 ───────────────────────────────────────────────────────────────────

    _STOPWORDS = frozenset([
        'a', 'an', 'the', 'is', 'it', 'in', 'on', 'at', 'to', 'for',
        'of', 'and', 'or', 'my', 'me', 'i', 'was', 'are', 'be', 'do',
        'did', 'has', 'have', 'had', 'what', 'when', 'how', 'which',
        'with', 'from', 'that', 'this', 'by', 'as', 'can', 'will',
    ])

    def _tokenize(self, text: str) -> List[str]:
        import re
        text = text.lower()
        text = re.sub(r'[-/]', ' ', text)
        text = re.sub(r'[^\w\s]', ' ', text)
        tokens = text.split()
        return [t for t in tokens if t not in self._STOPWORDS and len(t) > 1]

    def _bm25_scores(self, query: str, texts: List[str]) -> np.ndarray:
        try:
            from rank_bm25 import BM25Okapi
            tokenized_corpus = [self._tokenize(t) for t in texts]
            bm25             = BM25Okapi(tokenized_corpus)
            scores           = bm25.get_scores(self._tokenize(query))
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
        if matrix.shape[0] == 0:
            return np.zeros(0, dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm == 0:
            return np.zeros(matrix.shape[0], dtype=np.float32)
        norms  = np.linalg.norm(matrix, axis=1)
        norms  = np.where(norms == 0, 1e-9, norms)
        return (matrix @ q_vec) / (norms * q_norm)

    # ── Time decay (γ·Δ) ───────────────────────────────────────────────────────

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

    # ── Context-type boost (δ·Context) ────────────────────────────────────────

    def _apply_context_boost(
        self,
        scores:  np.ndarray,
        meta:    List[Dict],
        intent:  str,
    ) -> np.ndarray:
        """
        Add a small bonus when a chunk's document_type aligns with the
        detected query intent.  This implements δ·Context(D,i) from the
        unified Clinical Retrievability Score in the PhD proposal.
        """
        boosted_types = _INTENT_DOCTYPE_MAP.get(intent, [])
        if not boosted_types:
            return scores
        scores = scores.copy()
        for i, m in enumerate(meta):
            if m.get('doc_type', m.get('document_type', '')) in boosted_types:
                scores[i] = min(1.0, scores[i] + self.ctx_boost)
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
        lam       = self.mmr_lambda
        selected  = []
        remaining = list(valid)

        while remaining and len(selected) < k:
            if not selected:
                best_local = int(np.argmax([scores[i] for i in remaining]))
                chosen     = remaining[best_local]
            else:
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

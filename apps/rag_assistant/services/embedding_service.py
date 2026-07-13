# rag_assistant/services/embedding_service.py
"""
Embedding service — uses Gemini text-embedding-004 (free tier: 1,500 req/day).
Replaces sentence-transformers to eliminate the ~1 GB PyTorch memory footprint.

Key improvements vs earlier version:
- task_type="RETRIEVAL_DOCUMENT" at index time, "RETRIEVAL_QUERY" at query time
  (asymmetric embedding — measurably better retrieval quality per Google docs).
- Batch API: sends all texts in a single request instead of a per-text loop
  with sleep(). Indexing a 30-chunk PDF: ~30 s → ~1 s.
- Batches >100 items are split automatically (Gemini limit: 100 per request).
"""
import os
import logging
import numpy as np
from typing import List, Tuple, Dict, Any, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

_EMBED_DIM   = 768   # text-embedding-004 output dimension
_BATCH_LIMIT = 100   # Gemini batch API max items per request


class EmbeddingService:

    def __init__(self):
        self.cfg        = settings.RAG_CONFIG
        self.store_path = self.cfg['VECTOR_STORE_PATH']
        os.makedirs(self.store_path, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def embed(self, text: str) -> np.ndarray:
        """Embed a single query string (uses RETRIEVAL_QUERY task type)."""
        return self._call_api([text], task_type='RETRIEVAL_QUERY')[0]

    def embed_batch(self, texts: List[str], task_type: str = 'RETRIEVAL_DOCUMENT') -> np.ndarray:
        """
        Embed a list of texts in as few API calls as possible.

        task_type should be:
          'RETRIEVAL_DOCUMENT' — when indexing chunks (default)
          'RETRIEVAL_QUERY'    — when embedding a user query
        """
        if not texts:
            return np.zeros((0, _EMBED_DIM), dtype=np.float32)
        return self._call_api(texts, task_type=task_type)

    # ── Persist MedicalChunk embeddings ───────────────────────────────────────

    def embed_chunks(self, chunks) -> None:
        """Embed a queryset/list of MedicalChunk objects and save to DB."""
        from apps.rag_assistant.models import MedicalChunk
        chunk_list = list(chunks)
        if not chunk_list:
            return
        texts = [c.content for c in chunk_list]
        try:
            embeddings = self.embed_batch(texts, task_type='RETRIEVAL_DOCUMENT')
        except Exception as exc:
            logger.error('embed_chunks: embedding failed, skipping %d chunks: %s', len(chunk_list), exc)
            return
        to_update = []
        for chunk, vec in zip(chunk_list, embeddings):
            if np.any(vec):
                chunk.embedding = vec.astype(np.float32).tobytes()
                to_update.append(chunk)
        if to_update:
            MedicalChunk.objects.bulk_update(to_update, ['embedding'])

    # ── Load all embeddings for a patient ─────────────────────────────────────

    def load_patient_embeddings(
        self,
        patient,
        document_type: Optional[str] = None,
    ) -> Tuple[List[str], np.ndarray, List[Dict[str, Any]]]:
        from apps.rag_assistant.models import MedicalChunk
        qs = MedicalChunk.objects.filter(
            patient=patient, embedding__isnull=False
        ).select_related('document')
        if document_type:
            qs = qs.filter(document__document_type=document_type)

        chunks = list(qs)
        if not chunks:
            return [], np.zeros((0, _EMBED_DIM), dtype=np.float32), []

        texts, vecs, meta = [], [], []
        for c in chunks:
            try:
                raw = bytes(c.embedding)
                # Legacy rows were pickle-encoded; new rows use raw float32 bytes.
                if raw[:2] in (b'\x80\x03', b'\x80\x04', b'\x80\x05'):
                    import pickle
                    vec = pickle.loads(raw)
                else:
                    vec = np.frombuffer(raw, dtype=np.float32)
                texts.append(c.content)
                vecs.append(vec)
                meta.append({
                    'chunk_id':       str(c.id),
                    'document_id':    str(c.document_id),
                    'document_title': c.document.title,
                    'document_type':  c.document.document_type,
                    'chunk_index':    c.chunk_index,
                    'record_id':      str(c.document.record_id) if c.document.record_id else None,
                    'record_date':    c.metadata.get('record_date'),
                    **c.metadata,
                })
            except Exception as e:
                logger.warning('Bad embedding on chunk %s: %s', c.id, e)

        if not vecs:
            return [], np.zeros((0, _EMBED_DIM), dtype=np.float32), []

        return texts, np.vstack(vecs).astype(np.float32), meta

    # ── Internal ───────────────────────────────────────────────────────────────

    def _call_api(self, texts: List[str], task_type: str) -> np.ndarray:
        api_key = getattr(settings, 'GEMINI_API_KEY', '')
        if not api_key:
            raise RuntimeError('GEMINI_API_KEY is not configured — cannot embed texts')

        try:
            from google import genai
            from google.genai import types as genai_types
            client  = genai.Client(api_key=api_key)
            model   = self.cfg.get('EMBEDDING_MODEL', 'models/text-embedding-004')
            vectors = []

            # Split into batches of _BATCH_LIMIT (Gemini hard limit: 100)
            for start in range(0, len(texts), _BATCH_LIMIT):
                batch = texts[start:start + _BATCH_LIMIT]
                resp  = client.models.embed_content(
                    model    = model,
                    contents = batch,
                    config   = genai_types.EmbedContentConfig(task_type=task_type),
                )
                vectors.extend(e.values for e in resp.embeddings)

            return np.array(vectors, dtype=np.float32)

        except Exception as exc:
            raise RuntimeError(f'Gemini embedding error: {exc}') from exc

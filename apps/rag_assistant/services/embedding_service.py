# rag_assistant/services/embedding_service.py
"""
Embedding service — uses Gemini text-embedding-004 (free tier: 1,500 req/day).
Replaces sentence-transformers to eliminate the ~1 GB PyTorch memory footprint.
"""
import os
import logging
import time
import numpy as np
from typing import List, Tuple, Dict, Any, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

_EMBED_DIM = 768  # Gemini text-embedding-004 output dimension


class EmbeddingService:

    def __init__(self):
        self.cfg        = settings.RAG_CONFIG
        self.store_path = self.cfg['VECTOR_STORE_PATH']
        os.makedirs(self.store_path, exist_ok=True)

    # ── Single embedding ───────────────────────────────────────────────────────

    def embed(self, text: str) -> np.ndarray:
        results = self.embed_batch([text])
        return results[0]

    # ── Batch embed ────────────────────────────────────────────────────────────

    def embed_batch(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, _EMBED_DIM), dtype=np.float32)

        api_key = getattr(settings, 'GEMINI_API_KEY', '')
        if not api_key:
            raise RuntimeError('GEMINI_API_KEY is not configured — cannot embed texts')

        try:
            from google import genai
            client  = genai.Client(api_key=api_key)
            vectors = []
            for i, text in enumerate(texts):
                if i > 0 and i % 10 == 0:
                    time.sleep(1)  # free tier: ~1,500 req/day, stay safe
                resp = client.models.embed_content(
                    model    = 'models/text-embedding-004',
                    contents = text,
                )
                vectors.append(resp.embeddings[0].values)
            return np.array(vectors, dtype=np.float32)
        except Exception as exc:
            raise RuntimeError(f'Gemini embedding error: {exc}') from exc

    # ── Embed and persist MedicalChunk objects ─────────────────────────────────

    def embed_chunks(self, chunks) -> None:
        """Embed a queryset/list of MedicalChunk objects and save to DB."""
        from apps.rag_assistant.models import MedicalChunk
        chunk_list = list(chunks)
        if not chunk_list:
            return
        texts = [c.content for c in chunk_list]
        try:
            embeddings = self.embed_batch(texts)
        except Exception as exc:
            logger.error('embed_chunks: embedding failed, skipping %d chunks: %s', len(chunk_list), exc)
            return
        to_update = []
        for chunk, vec in zip(chunk_list, embeddings):
            if np.any(vec):  # skip all-zero vectors — they indicate a failed embedding
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

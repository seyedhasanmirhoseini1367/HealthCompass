# rag_assistant/services/embedding_service.py
"""
Embedding service — generates and stores sentence-transformer vectors
for medical record chunks.  Identical contract to PersonalPortfolio but
scoped per patient and stored in MedicalChunk.embedding (pickle bytes).
"""
import os
import pickle
import logging
import numpy as np
from typing import List, Tuple, Dict, Any, Optional

from django.conf import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    _model = None  # class-level cache — loaded once per process

    def __init__(self):
        self.cfg        = settings.RAG_CONFIG
        self.model_name = self.cfg['EMBEDDING_MODEL']
        self.store_path = self.cfg['VECTOR_STORE_PATH']
        os.makedirs(self.store_path, exist_ok=True)

    @property
    def model(self):
        if EmbeddingService._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                EmbeddingService._model = SentenceTransformer(self.model_name)
                logger.info('Loaded embedding model: %s', self.model_name)
            except Exception as e:
                logger.error('Failed to load embedding model %s: %s', self.model_name, e)
                EmbeddingService._model = None
        return EmbeddingService._model

    # ── Single embedding ───────────────────────────────────────────────────────

    def embed(self, text: str) -> np.ndarray:
        if self.model is None:
            return np.zeros(384, dtype=np.float32)
        return self.model.encode(text, convert_to_numpy=True).astype(np.float32)

    # ── Batch embed ────────────────────────────────────────────────────────────

    def embed_batch(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 384), dtype=np.float32)
        if self.model is None:
            return np.zeros((len(texts), 384), dtype=np.float32)
        return self.model.encode(
            texts,
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).astype(np.float32)

    # ── Embed and persist MedicalChunk objects ─────────────────────────────────

    def embed_chunks(self, chunks) -> None:
        """Embed a queryset/list of MedicalChunk objects and save to DB."""
        from rag_assistant.models import MedicalChunk
        chunk_list = list(chunks)
        if not chunk_list:
            return
        texts      = [c.content for c in chunk_list]
        embeddings = self.embed_batch(texts)
        for chunk, vec in zip(chunk_list, embeddings):
            chunk.embedding = pickle.dumps(vec)
        MedicalChunk.objects.bulk_update(chunk_list, ['embedding'])

    # ── Load all embeddings for a patient ─────────────────────────────────────

    def load_patient_embeddings(
        self,
        patient,
        document_type: Optional[str] = None,
    ) -> Tuple[List[str], np.ndarray, List[Dict[str, Any]]]:
        """
        Return (texts, embeddings_matrix, metadata_list) for all embedded
        chunks belonging to *patient*.  Optionally filter by document_type.
        """
        from rag_assistant.models import MedicalChunk
        qs = MedicalChunk.objects.filter(
            patient=patient, embedding__isnull=False
        ).select_related('document')
        if document_type:
            qs = qs.filter(document__document_type=document_type)

        chunks = list(qs)
        if not chunks:
            return [], np.zeros((0, 384), dtype=np.float32), []

        texts, vecs, meta = [], [], []
        for c in chunks:
            try:
                vec = pickle.loads(bytes(c.embedding))
                texts.append(c.content)
                vecs.append(vec)
                meta.append({
                    'chunk_id':      str(c.id),
                    'document_id':   str(c.document_id),
                    'document_title': c.document.title,
                    'document_type': c.document.document_type,
                    'chunk_index':   c.chunk_index,
                    'record_id':     str(c.document.record_id) if c.document.record_id else None,
                    'record_date':   c.metadata.get('record_date'),
                    **c.metadata,
                })
            except Exception as e:
                logger.warning('Bad embedding on chunk %s: %s', c.id, e)

        if not vecs:
            return [], np.zeros((0, 384), dtype=np.float32), []

        return texts, np.vstack(vecs).astype(np.float32), meta

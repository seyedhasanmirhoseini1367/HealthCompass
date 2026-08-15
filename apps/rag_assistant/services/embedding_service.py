# rag_assistant/services/embedding_service.py
"""
Embedding service — uses Gemini gemini-embedding-001 (3072-dim).

text-embedding-004 (768-dim) was deprecated; gemini-embedding-001 replaces it.
Existing chunks stored with the old 768-dim model need re-indexing before
cosine similarity will work correctly against the new query vectors.

Key features:
- task_type="RETRIEVAL_DOCUMENT" at index time, "RETRIEVAL_QUERY" at query time
  (asymmetric embedding — better retrieval quality per Google docs).
- Batch API: sends all texts in a single request.
- Batches >100 items are split automatically (Gemini limit: 100 per request).
"""
import os
import logging
from collections import Counter
from typing import List, Tuple, Dict, Any, Optional

import numpy as np
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count
from django.utils import timezone

logger = logging.getLogger(__name__)

from healthcompass.observability import Event as OpsEvent, emit as ops_emit

_EMBED_DIM   = 3072  # default gemini-embedding-001 output dimension
_BATCH_LIMIT = 100   # Gemini batch API max items per request


class OutboundEmbeddingBlocked(RuntimeError):
    """
    The call was refused before leaving the process, not attempted and failed.

    Distinct from a provider error so that "we chose not to send this" is never
    read as "the provider is down" — the two call for opposite responses.
    """


# ── Embedding provenance & compatibility ──────────────────────────────────────
#
# A stored vector is only meaningful against a query vector from the *same*
# model. Comparing across models still yields a cosine number, so incompatibility
# is invisible unless it is checked explicitly. These helpers are the single
# source of truth for "what model are we on, and does this row match it?".

COMPAT_OK             = 'ok'
COMPAT_UNKNOWN        = 'unknown'          # legacy row, no provenance recorded
COMPAT_MODEL_MISMATCH = 'model_mismatch'
COMPAT_DIM_MISMATCH   = 'dim_mismatch'


def active_embedding_model() -> str:
    """Model configured for indexing and querying right now."""
    model = settings.RAG_CONFIG.get('EMBEDDING_MODEL', '')
    if not model:
        raise ImproperlyConfigured(
            "RAG_CONFIG['EMBEDDING_MODEL'] is not set. Refusing to embed rather "
            "than falling back to a default that may be deprecated."
        )
    return model


def active_embedding_dim() -> int:
    """Expected vector length for the active model."""
    return int(settings.RAG_CONFIG.get('EMBEDDING_DIM', _EMBED_DIM))


def strict_provenance() -> bool:
    """When True, unknown-provenance rows are excluded from retrieval."""
    return bool(getattr(settings, 'EMBEDDING_STRICT_PROVENANCE', False))


def classify_embedding(
    stored_model: str,
    stored_dim: Optional[int],
    actual_dim: Optional[int] = None,
    *,
    active_model: Optional[str] = None,
    active_dim: Optional[int] = None,
    strict: Optional[bool] = None,
) -> str:
    """
    Return one of the COMPAT_* constants for a single stored embedding.

    Dimension is checked before model so that a legacy row of the wrong size is
    reported as a dimension mismatch (its historical classification) rather than
    being reclassified by the newer model check.

    `actual_dim` is the length of the decoded vector when available; it takes
    precedence over the recorded `stored_dim`, because the bytes on disk are
    the ground truth and the column is only an annotation.
    """
    active_model = active_model if active_model is not None else active_embedding_model()
    active_dim   = active_dim   if active_dim   is not None else active_embedding_dim()
    strict       = strict       if strict       is not None else strict_provenance()

    dim = actual_dim if actual_dim is not None else stored_dim
    if dim is not None and int(dim) != int(active_dim):
        return COMPAT_DIM_MISMATCH

    # Coerce defensively: callers may pass values off ORM objects or mocks.
    stored_model = stored_model if isinstance(stored_model, str) else ''
    if not stored_model:
        # Pre-provenance row. Its dimension already matches the active model, so
        # it is usable unless the deployment has opted into strict enforcement.
        return COMPAT_UNKNOWN if strict else COMPAT_OK

    if stored_model != active_model:
        return COMPAT_MODEL_MISMATCH

    return COMPAT_OK


def audit_embeddings(model_cls, queryset=None) -> Dict[str, Any]:
    """
    Summarise embedding provenance for a chunk model without decoding vectors.

    Returns counts keyed by COMPAT_* status plus a per-(model, dimensions)
    breakdown, so an operator can see exactly what is in the index and decide
    whether a re-embed is warranted. Purely read-only.
    """
    qs = model_cls.objects.all() if queryset is None else queryset
    active_model = active_embedding_model()
    active_dim   = active_embedding_dim()
    strict       = strict_provenance()

    total     = qs.count()
    unembedded = qs.filter(embedding__isnull=True).count()

    breakdown, statuses = [], Counter()
    rows = (qs.exclude(embedding__isnull=True)
              .values('embedding_model', 'embedding_dimensions')
              .annotate(count=Count('*'))
              .order_by('-count'))
    for row in rows:
        status = classify_embedding(
            stored_model = row['embedding_model'] or '',
            stored_dim   = row['embedding_dimensions'],
            active_model = active_model,
            active_dim   = active_dim,
            strict       = strict,
        )
        statuses[status] += row['count']
        breakdown.append({
            'embedding_model':      row['embedding_model'] or '(unrecorded)',
            'embedding_dimensions': row['embedding_dimensions'],
            'count':                row['count'],
            'status':               status,
        })

    stale = sum(c for s, c in statuses.items() if s != COMPAT_OK)
    return {
        'model':            model_cls.__name__,
        'active_model':     active_model,
        'active_dim':       active_dim,
        'strict':           strict,
        'total':            total,
        'unembedded':       unembedded,
        'compatible':       statuses.get(COMPAT_OK, 0),
        'stale':            stale,
        'by_status':        dict(statuses),
        'breakdown':        breakdown,
    }


def StatusEnum(chunk):
    """
    The EmbeddingStatus choices for whichever chunk model this row belongs to.

    Both MedicalChunk and GeneralKnowledgeChunk inherit it from
    EmbeddingProvenanceMixin; reading it off the instance keeps this module free
    of a hardcoded model reference.
    """
    return type(chunk).EmbeddingStatus


def _log_incompatible(source: str, skipped: Counter) -> None:
    """Emit one aggregated warning per load instead of one line per bad row."""
    if not skipped:
        return
    detail = ', '.join(f'{count} {reason}' for reason, count in sorted(skipped.items()))
    logger.warning(
        '%s: excluded %d incompatible embedding(s) from retrieval (%s). '
        'Active model=%s dim=%d. Run `manage.py embedding_status` for a breakdown.',
        source, sum(skipped.values()), detail,
        active_embedding_model(), active_embedding_dim(),
    )


class EmbeddingService:

    def __init__(self):
        self.cfg        = settings.RAG_CONFIG
        self.store_path = self.cfg['VECTOR_STORE_PATH']
        os.makedirs(self.store_path, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def embed(self, text: str, user=None) -> np.ndarray:
        """
        Embed a single query string (uses RETRIEVAL_QUERY task type).

        `user` is the person whose question this is. Passing it lets the egress
        guard refuse before the text reaches Google; it defaults to None for
        callers embedding non-patient text (e.g. public knowledge-base articles).
        """
        from apps.accounts.egress import ExternalProcessingGuard

        if user is not None:
            ExternalProcessingGuard.check(user, 'rag.embed_query')
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
        """
        Embed a queryset/list of MedicalChunk objects and save to DB.

        Every vector written here is stamped with the model that produced it, so
        a later model change is detectable per row rather than only inferable
        from whatever RAG_CONFIG happens to say at the time.
        """
        from apps.accounts.egress import ExternalProcessingGuard
        from apps.rag_assistant.models import MedicalChunk
        chunk_list = list(chunks)
        if not chunk_list:
            return

        # This is reached from the post_save signal when a record is uploaded,
        # not from the assistant, so RAGService's gate never sees it. Without a
        # check here the text of every uploaded record goes to Google whether or
        # not the patient agreed to external processing.
        owner = getattr(chunk_list[0], 'patient', None)
        if not ExternalProcessingGuard.allows(owner, 'rag.embed_documents'):
            logger.info(
                'embed_chunks: skipping %d chunk(s) — external processing not permitted',
                len(chunk_list),
            )
            # BLOCKED, not FAILED: refusing to send data is the system working
            # as intended. It is recorded so the chunks are not mistaken for a
            # transient failure and retried against the patient's wishes.
            self._mark(chunk_list, status=StatusEnum(chunk_list[0]).BLOCKED,
                       error='external processing not permitted')
            return

        model    = active_embedding_model()
        version  = self.cfg.get('EMBEDDING_MODEL_VERSION', '')

        # Embed in provider-sized batches and persist after EACH one.
        #
        # Previously the whole list went through a single embed_batch() call
        # that internally looped batches of _BATCH_LIMIT; a failure in any batch
        # raised out of the entire call, so vectors already computed for earlier
        # batches were thrown away and their quota spent for nothing. Persisting
        # per batch means a mid-run failure keeps everything already earned.
        for start in range(0, len(chunk_list), _BATCH_LIMIT):
            batch = chunk_list[start:start + _BATCH_LIMIT]
            try:
                embeddings = self.embed_batch([c.content for c in batch],
                                              task_type='RETRIEVAL_DOCUMENT')
            except OutboundEmbeddingBlocked as exc:
                # Not an incident. Nobody attempted anything and nothing broke —
                # this deployment is configured not to send text to the provider,
                # exactly as the consent refusal above is configured. Alerting on
                # it would train operators to ignore EMBEDDING_FAILED, which is
                # the event that does mean something.
                #
                # Still not silent: the status and the reason go on the row, so
                # these chunks are findable as unretrievable rather than looking
                # merely unprocessed.
                logger.info(
                    'embed_chunks: not embedding %d chunk(s) — outbound embedding disabled',
                    len(batch))
                self._mark(batch, status=StatusEnum(batch[0]).FAILED, error=str(exc)[:300])
                continue
            except Exception as exc:
                logger.error(
                    'embed_chunks: embedding failed for %d chunk(s): %s', len(batch), exc)
                # Structured event: these chunks are now unretrievable until a
                # retry succeeds, which is a patient-impacting condition and has
                # to be alertable rather than buried in prose.
                ops_emit(OpsEvent.EMBEDDING_FAILED,
                         chunks=len(batch),
                         patient_id=getattr(batch[0], 'patient_id', None),
                         error_type=type(exc).__name__)
                self._mark(batch, status=StatusEnum(batch[0]).FAILED, error=str(exc)[:300])
                continue

            self._persist_batch(batch, embeddings, model, version)
        return

    # ── Indexing-state bookkeeping ────────────────────────────────────────────

    def _mark(self, chunks, *, status, error: str = '') -> None:
        """Record an indexing outcome without touching any stored vector."""
        stamped = timezone.now()
        for c in chunks:
            c.embedding_status     = status
            c.embedding_error      = error
            c.embedding_attempts   = (c.embedding_attempts or 0) + 1
            c.embedding_attempted_at = stamped
        type(chunks[0]).objects.bulk_update(
            chunks,
            ['embedding_status', 'embedding_error',
             'embedding_attempts', 'embedding_attempted_at'],
        )

    def _persist_batch(self, batch, embeddings, model: str, version: str) -> None:
        """
        Save one batch. Chunks the provider did not return a usable vector for
        are marked FAILED rather than left silently NULL — a shorter response
        than the request used to be dropped by zip() with no error at all.
        """
        stamped = timezone.now()
        embedded, unusable = [], []
        vectors = list(embeddings)
        for i, chunk in enumerate(batch):
            vec = vectors[i] if i < len(vectors) else None
            if vec is None or not np.any(vec):
                unusable.append(chunk)
                continue
            chunk.embedding               = vec.astype(np.float32).tobytes()
            chunk.embedding_model         = model
            chunk.embedding_model_version = version
            chunk.embedding_dimensions    = int(len(vec))
            chunk.embedded_at             = stamped
            chunk.embedding_status        = StatusEnum(chunk).EMBEDDED
            chunk.embedding_error         = ''
            chunk.embedding_attempts      = (chunk.embedding_attempts or 0) + 1
            chunk.embedding_attempted_at  = stamped
            embedded.append(chunk)

        if unusable:
            logger.error(
                'embed_chunks: provider returned no usable vector for %d chunk(s)',
                len(unusable))
            ops_emit(OpsEvent.EMBEDDING_NO_VECTOR, chunks=len(unusable),
                     patient_id=getattr(unusable[0], 'patient_id', None))
            self._mark(unusable, status=StatusEnum(unusable[0]).FAILED,
                       error='provider returned no usable vector')

        if embedded:
            # type(...) rather than a hardcoded MedicalChunk: the same path is
            # used for GeneralKnowledgeChunk, which shares the provenance mixin.
            type(embedded[0]).objects.bulk_update(embedded, [
                'embedding', 'embedding_model', 'embedding_model_version',
                'embedding_dimensions', 'embedded_at',
                'embedding_status', 'embedding_error',
                'embedding_attempts', 'embedding_attempted_at',
            ])

    # ── Load all embeddings for a patient ─────────────────────────────────────

    def load_patient_embeddings(
        self,
        patient,
        document_type=None,
    ) -> Tuple[List[str], np.ndarray, List[Dict[str, Any]]]:
        """
        `document_type` accepts a single type or an iterable of types.

        Several types are needed because a clinical fact is not confined to the
        document class that introduced it: a medication's *status* is usually
        recorded in a note ("metformin discontinued"), not in the prescription.
        Filtering to one type excluded that evidence before ranking could see it.

        The patient filter is applied unconditionally and independently, so
        widening the type never widens the patient boundary.
        """
        from apps.rag_assistant.models import MedicalChunk
        qs = MedicalChunk.objects.filter(
            patient=patient, embedding__isnull=False
        ).select_related('document')

        # Chunks with no vector are correctly excluded — they are not indexed —
        # but that exclusion used to be completely silent, so a record could be
        # invisible to retrieval with nothing anywhere to say so. Provenance
        # mismatches were already reported this way; unembedded chunks were not.
        unembedded = MedicalChunk.objects.filter(
            patient=patient, embedding__isnull=True)
        if document_type:
            if isinstance(document_type, (list, tuple, set, frozenset)):
                unembedded = unembedded.filter(document__document_type__in=list(document_type))
            else:
                unembedded = unembedded.filter(document__document_type=document_type)
        pending_or_failed = unembedded.count()
        if pending_or_failed:
            logger.warning(
                'load_patient_embeddings: %d chunk(s) are not retrievable because they '
                'have no embedding. Run `manage.py retry_failed_embeddings` to recover them.',
                pending_or_failed,
            )
            ops_emit(OpsEvent.RETRIEVAL_MISSING_EMBEDDINGS, level=logging.WARNING,
                     chunks=pending_or_failed, patient_id=getattr(patient, 'pk', None))
        if document_type:
            if isinstance(document_type, (list, tuple, set, frozenset)):
                qs = qs.filter(document__document_type__in=list(document_type))
            else:
                qs = qs.filter(document__document_type=document_type)

        active_model = active_embedding_model()
        active_dim   = active_embedding_dim()
        strict       = strict_provenance()

        chunks = list(qs)
        if not chunks:
            return [], np.zeros((0, active_dim), dtype=np.float32), []

        texts, vecs, meta = [], [], []
        skipped = Counter()
        for c in chunks:
            try:
                raw = bytes(c.embedding)
                # Legacy rows were pickle-encoded; new rows use raw float32 bytes.
                if raw[:2] in (b'\x80\x03', b'\x80\x04', b'\x80\x05'):
                    import pickle
                    vec = pickle.loads(raw)
                else:
                    vec = np.frombuffer(raw, dtype=np.float32)

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

        _log_incompatible('load_patient_embeddings', skipped)

        if not vecs:
            return [], np.zeros((0, active_dim), dtype=np.float32), []

        return texts, np.vstack(vecs).astype(np.float32), meta

    # ── Internal ───────────────────────────────────────────────────────────────

    def _call_api(self, texts: List[str], task_type: str) -> np.ndarray:
        # The one place text leaves this process for the embedding provider, and
        # therefore the one place the door can be shut. Under the test runner it
        # is shut by default: the inline auto-indexer would otherwise embed every
        # fixture record against the real API key. See RAG_ALLOW_EXTERNAL_EMBEDDING.
        if not getattr(settings, 'RAG_ALLOW_EXTERNAL_EMBEDDING', True):
            raise OutboundEmbeddingBlocked(
                'Outbound embedding is disabled (RAG_ALLOW_EXTERNAL_EMBEDDING=False). '
                'Patch EmbeddingService._call_api or embed_batch in tests that need vectors.'
            )

        api_key = getattr(settings, 'GEMINI_API_KEY', '')
        if not api_key:
            raise RuntimeError('GEMINI_API_KEY is not configured — cannot embed texts')

        try:
            from google import genai
            from google.genai import types as genai_types
            # Indexing runs off-request, but an untimed call still pins the
            # background thread forever if the provider hangs.
            timeout_ms = int(self.cfg.get('EMBED_TIMEOUT', 60)) * 1000
            try:
                client = genai.Client(api_key=api_key,
                                      http_options={'timeout': timeout_ms})
            except TypeError:
                client = genai.Client(api_key=api_key)
            # Never fall back to a hardcoded model name here: the previous
            # default was text-embedding-004, which was later deprecated, so a
            # missing config key would have silently produced 768-dim vectors.
            model   = active_embedding_model()
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

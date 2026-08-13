"""
Structured operational events for failures that would otherwise be silent.

Motivation, from three defects found in this codebase:

  * XML hardening fell back to the unsafe stdlib parser with no error and no log.
  * Critical-value detection ran on one ingestion path out of three, silently.
  * Embedding failures left records permanently unretrievable, logged once at
    ERROR and then forgotten among thousands of other lines.

Each was invisible in production and found only by reading code. Ordinary log
lines were not enough: they are unstructured prose, so nothing can alert on them
reliably.

An operational event is a **stable machine-readable code** plus scalar context.
A log drain, alerting rule or error tracker keys on `event=<CODE>` without having
to parse English.

PHI safety is enforced, not merely intended
-------------------------------------------
`emit()` accepts scalars only — ints, floats, bools, None, UUIDs and short
identifier-like strings. Any string that looks like content (long, or containing
newlines) is rejected with a `ValueError` in DEBUG and replaced with a redaction
marker in production. That makes "log the chunk text to help debug this" fail
loudly during development instead of quietly shipping patient data to a log
aggregator.

Deliberately NOT introduced here: an external error-tracking service. Choosing a
vendor is a deployment decision, and the architecture does not require one to
make these events observable — see docs/OBSERVABILITY.md for wiring options.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

logger = logging.getLogger('healthcompass.ops')

#: Longest string accepted as context. Identifiers and codes are short; clinical
#: content is not. The limit is what makes accidental PHI logging fail.
MAX_SCALAR_LEN = 64

REDACTED = '<redacted:non-scalar>'


# ── Event codes ───────────────────────────────────────────────────────────────
#
# Stable strings. Alerting rules key on these, so renaming one is a breaking
# change for whoever is on call.

class Event:
    #: An embedding attempt failed; chunks are unretrievable until retried.
    EMBEDDING_FAILED = 'EMBEDDING_FAILED'
    #: The provider returned no usable vector for chunks it was asked about.
    EMBEDDING_NO_VECTOR = 'EMBEDDING_NO_VECTOR'
    #: Background indexing raised; the record may never become searchable.
    INDEXING_FAILED = 'INDEXING_FAILED'
    #: A document failed to parse during ingestion.
    INGESTION_PARSE_FAILED = 'INGESTION_PARSE_FAILED'
    #: An uploaded XML document was rejected by the hardened parser.
    UNSAFE_DOCUMENT_REJECTED = 'UNSAFE_DOCUMENT_REJECTED'
    #: A clinical alert could not be created — the patient is NOT notified.
    ALERT_CREATION_FAILED = 'ALERT_CREATION_FAILED'
    #: Every LLM provider failed; the user received the fallback message.
    LLM_ALL_PROVIDERS_FAILED = 'LLM_ALL_PROVIDERS_FAILED'
    #: A right-to-erasure request left files behind. Legally significant.
    ERASURE_INCOMPLETE = 'ERASURE_INCOMPLETE'
    #: Retrieval excluded chunks that have no embedding.
    RETRIEVAL_MISSING_EMBEDDINGS = 'RETRIEVAL_MISSING_EMBEDDINGS'
    #: PHI was disclosed but the access trail could not be written.
    ACCESS_LOG_FAILED = 'ACCESS_LOG_FAILED'
    #: One inference took long enough to be worth looking at.
    INFERENCE_SLOW = 'INFERENCE_SLOW'


#: Events that mean a patient may be seeing incomplete or missing medical
#: information. These deserve alerting, not just logging.
PATIENT_IMPACTING = frozenset({
    Event.EMBEDDING_FAILED,
    Event.EMBEDDING_NO_VECTOR,
    Event.INDEXING_FAILED,
    Event.INGESTION_PARSE_FAILED,
    Event.ALERT_CREATION_FAILED,
    Event.RETRIEVAL_MISSING_EMBEDDINGS,
    Event.ERASURE_INCOMPLETE,
    Event.ACCESS_LOG_FAILED,
})


def _safe_scalar(value: Any) -> Any:
    """
    Allow identifiers and counts; refuse anything that could carry content.

    Raises in DEBUG so the mistake is caught in development, and redacts in
    production so a logging bug can never become a PHI incident.
    """
    if value is None or isinstance(value, (int, float, bool, uuid.UUID)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_SCALAR_LEN and '\n' not in value:
            return value
        from django.conf import settings
        if getattr(settings, 'DEBUG', False):
            raise ValueError(
                f'Refusing to log a value of length {len(value)} as operational '
                f'context. Operational events carry codes and counts, never '
                f'clinical content. Log an identifier or a count instead.'
            )
        return REDACTED
    return REDACTED


def emit(code: str, *, level: int = logging.ERROR, **context: Any) -> Dict[str, Any]:
    """
    Record one operational event.

    Returns the sanitised payload so callers (and tests) can assert on exactly
    what was recorded.
    """
    payload = {key: _safe_scalar(val) for key, val in context.items()}
    payload['event'] = code
    payload['patient_impacting'] = code in PATIENT_IMPACTING

    detail = ' '.join(f'{k}={v}' for k, v in sorted(payload.items()) if k != 'event')
    logger.log(level, 'event=%s %s', code, detail, extra={'ops_event': payload})
    return payload

"""
Client-safe error reporting.

Internal exception text must not reach a browser or an API client. Provider SDK
errors routinely embed request URLs, model identifiers, organisation ids and
occasionally partial key material; database errors embed connection details.

`healthcompass/urls.py readiness()` already gets this right — "The exception text
can carry connection strings; log it, do not return it" — but six other call
sites put `str(exc)` straight into a response body.

The correlation id is what keeps this debuggable: the client is given an opaque
reference, the full traceback is logged against that same reference, and support
can join the two without the user ever seeing internals.
"""
import logging
import uuid

logger = logging.getLogger(__name__)

#: Deliberately generic. Anything more specific risks describing internals.
GENERIC_MESSAGE = 'Something went wrong on our side. Please try again.'


def client_error(exc: Exception, *, context: str = '', log: logging.Logger = None) -> dict:
    """
    Log `exc` in full and return a body safe to send to a client.

    Returns {'error': <generic text>, 'reference': <uuid>} — the reference
    appears in both the log line and the response, and nothing else does.
    """
    reference = uuid.uuid4().hex[:12]
    # exc_info=exc rather than logger.exception(): the latter reads the ambient
    # sys.exc_info(), so calling this outside an `except` block would log
    # "NoneType: None" and lose the error entirely — withheld from the client
    # AND absent from the log. Passing the exception explicitly works in both
    # cases.
    (log or logger).error(
        'Unhandled error [ref=%s]%s', reference,
        f' in {context}' if context else '', exc_info=exc)
    return {'error': GENERIC_MESSAGE, 'reference': reference}

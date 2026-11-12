"""
Background jobs (audit ARCH-02).

One Redis-backed RQ setup with two queues: 'ingest' (parsing, OCR, LLM
structuring, indexing) and 'default' (scheduled maintenance). The worker is a
separate process: `python manage.py rqworker ingest default`.

`enqueue()` is the only entry point. It runs the job inline (synchronously, in
the caller) when RQ_ASYNC is False, which is the case under the test runner,
without a CACHE_URL (local development), and whenever RAG_AUTO_INDEX_SYNC is
set (kept as an alias for the older setting). Callers never branch.

Jobs are passed primary keys, never model instances: the worker reads the
current row, and nothing stale or large is pickled into Redis.
"""
import logging

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


def is_async() -> bool:
    if getattr(settings, 'RAG_AUTO_INDEX_SYNC', False):
        return False
    return bool(getattr(settings, 'RQ_ASYNC', False))


def enqueue(func, *args, queue: str = 'default', on_commit: bool = True, **kwargs):
    """Run *func* in the background worker, or inline when not async.

    * RAG_AUTO_INDEX_SYNC (the test runner's default): inline, immediately.
    * No worker (RQ_ASYNC False): inline once the transaction commits.
    * Worker: enqueued once the transaction commits, so it can see the rows.
    """
    if getattr(settings, 'RAG_AUTO_INDEX_SYNC', False):
        return func(*args, **kwargs)
    if not is_async():
        if on_commit:
            transaction.on_commit(lambda: func(*args, **kwargs))
            return None
        return func(*args, **kwargs)

    def _submit():
        import django_rq
        django_rq.get_queue(queue).enqueue(func, *args, **kwargs,
                                           job_timeout=getattr(settings, 'RQ_JOB_TIMEOUT', 600))

    if on_commit:
        transaction.on_commit(_submit)
    else:
        _submit()
    return None

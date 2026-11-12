"""
Per-account login throttling (audit SEC-06).

The per-IP limit stops one address guessing many accounts; this stops many
addresses guessing one account. After MAX_FAILURES failed attempts for an
identifier within WINDOW_SECONDS, further attempts for it are refused from any
address until the window passes.

Counted for every identifier, registered or not, and the refusal is the same
either way, so it does not reveal whether an account exists. The counter is
keyed on a hash of the normalised identifier, never the identifier itself.
"""
import hashlib

from django.core.cache import cache

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
MESSAGE = 'Too many failed sign-in attempts for this account. Please wait 15 minutes and try again.'


def _key(identifier: str) -> str:
    norm = (identifier or '').strip().lower()
    return 'login_fail:' + hashlib.sha256(norm.encode()).hexdigest()[:32]


def is_locked(identifier: str) -> bool:
    return bool(identifier) and cache.get(_key(identifier), 0) >= MAX_FAILURES


def record_failure(identifier: str) -> None:
    if not identifier:
        return
    key = _key(identifier)
    if cache.add(key, 1, WINDOW_SECONDS):
        return
    try:
        cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, WINDOW_SECONDS)


def clear(identifier: str) -> None:
    cache.delete(_key(identifier))

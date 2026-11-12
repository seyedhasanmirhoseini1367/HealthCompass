"""Username generation for accounts created from an email address.

Usernames never contain '@' (audit SEC-02): an email-shaped username could be
mistaken for someone else's address by any lookup that accepts either.
"""
import re
import secrets

_ALLOWED = re.compile(r'[^\w.+-]')


def generate_username(email: str) -> str:
    """Email local part, cleaned, plus a short random suffix. Not guaranteed
    unique; callers retry on IntegrityError."""
    local = (email or '').split('@', 1)[0]
    base = _ALLOWED.sub('', local)[:20] or 'user'
    return f'{base}_{secrets.token_hex(3)}'

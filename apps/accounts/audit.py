"""
Recording administrative actions.

One helper, called from the handful of places that change what the system will
permit. Not a framework and not a signal: an audit line that fires
automatically from a signal records what the ORM did, while what matters here is
what a person decided — including the decisions that were refused, which no
signal ever sees because nothing was written.

PHI safety is enforced rather than intended, on the same principle as
`healthcompass.observability`: metadata takes scalars only, so "log the record
so we can debug this" fails here instead of quietly filing clinical content into
a table that compliance staff can read.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: Longest string accepted in metadata. Identifiers and reasons are short;
#: clinical content is not, and the limit is what makes the difference
#: enforceable rather than a matter of care.
MAX_SCALAR_LEN = 120


def _safe(value: Any) -> Any:
    import uuid

    if value is None or isinstance(value, (int, float, bool, uuid.UUID)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_SCALAR_LEN and '\n' not in value:
            return value
        return '<redacted:non-scalar>'
    return '<redacted:non-scalar>'


def actor_label_for(user) -> str:
    """Username and role as they were at the time of the action."""
    if user is None or not getattr(user, 'pk', None):
        return ''
    role = getattr(user, 'role', '') or ''
    return f'{user.username} ({role})' if role else user.username


def authority_of(user) -> str:
    """
    Which authority the action relied on.

    With a single administrator this is nearly always 'superuser'. It is
    recorded now so the trail stays readable if that stops being true, not
    because a permission system exists to describe.
    """
    if user is None:
        return ''
    if getattr(user, 'is_superuser', False):
        return 'superuser'
    if getattr(user, 'is_staff', False):
        return 'staff'
    return getattr(user, 'role', '') or 'authenticated'


def record(action: str, *, actor=None, target=None, target_label: str = '',
           success: bool = True, **metadata) -> None:
    """
    Append one administrative audit row.

    Best-effort at the boundary: an audit failure must not turn a completed
    administrative action into a 500, but it must not pass silently either, so
    it is logged. The alternative — raising — would make the audit table a
    single point of failure for administration itself.
    """
    from apps.accounts.models import AdminAuditEvent

    target_type = target_id = ''
    if target is not None:
        target_type = target.__class__.__name__
        target_id = str(getattr(target, 'pk', '') or '')

    try:
        AdminAuditEvent.objects.create(
            actor=actor if getattr(actor, 'pk', None) else None,
            actor_label=actor_label_for(actor),
            action=action,
            target_type=target_type[:64],
            target_id=target_id[:64],
            target_label=_safe(target_label) if target_label else '',
            authority=authority_of(actor)[:64],
            success=success,
            metadata={key: _safe(val) for key, val in metadata.items()},
        )
    except Exception as exc:
        logger.error('Could not record admin audit event %s by %s: %s',
                     action, getattr(actor, 'pk', None), exc)

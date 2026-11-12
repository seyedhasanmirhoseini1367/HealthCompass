"""Helpers for parsing query-string parameters without raising."""
from datetime import date


def parse_date_param(value) -> date | None:
    """ISO 'YYYY-MM-DD' to a date, or None for anything missing or malformed.

    Query strings are user input; a raw string handed to a DateField filter
    raises ValidationError, which surfaced as a 500 (audit CQ-03).
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None

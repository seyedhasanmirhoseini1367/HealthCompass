"""
Population figures for everyone, without exposing anyone.

An average or a count over a handful of people is not anonymous: "average
creatinine across 3 patients", plus knowing two of them, gives the third. So
for people without research/admin access (can_view_population_analytics),
every figure based on fewer than MIN_PATIENTS distinct patients is withheld.
Staff, admins and data scientists keep the full view.
"""
from __future__ import annotations

from django.conf import settings


def min_patients() -> int:
    return int(getattr(settings, 'POPULATION_MIN_PATIENTS', 10))


def enough(patients: int) -> bool:
    return patients >= min_patients()


def public_biomarkers(pop_trending: dict, pop_latest: dict) -> tuple[dict, dict]:
    """Drop every point, series and latest value describing too few patients."""
    trending = {}
    for name, series in pop_trending.items():
        kept = [p for p in series if enough(p.get('patients', 0))]
        if len(kept) >= 2:
            trending[name] = kept
    latest = {name: v for name, v in pop_latest.items() if enough(v.get('patients', 0))}
    return trending, latest

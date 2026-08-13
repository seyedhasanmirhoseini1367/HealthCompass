"""
The clinical fact layer: one definition of "a lab value", and typed answers.

Why this module exists
----------------------
Two independent readers of ParsedLabValue had grown up side by side and
disagreed in four ways — which rows they selected, which date they treated as
the observation date, when they considered two values comparable, and what they
did with several readings on one date. `TrajectoryService` filtered
`unit_known=True` at the query, used the record date only, re-normalised
unrecognised units on the fly, and **deduplicated same-date rows, discarding
every reading but the first**. `conflict_service` did none of those things. Two
definitions of the same clinical fact will always drift; this is the one.

The second, larger reason: callers received a bare value. A bare value cannot
express "there is nothing on file", and it cannot express "two documents
disagree" — so callers silently treated missing as normal, and something,
somewhere, had to pick a winner between conflicting readings. Every query here
returns one of three explicit results instead:

    Confirmed   one observation, or several that agree
    Conflicted  several that disagree — ALL of them, with their sources
    Absent      nothing usable, with a machine-readable reason

There is no fourth case and no bare value to fall back to, so a caller cannot
accidentally choose. That is the entire point.

Deliberately NOT here
---------------------
No thresholds, no reference ranges, no staleness rules, no clinical
significance. This layer reports what the records say and where each statement
came from. Judgement belongs above it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Reasons a fact is absent ──────────────────────────────────────────────────
#
# Machine-readable, because "no data" and "data exists but is unusable" are
# different clinical situations and the caller must be able to tell them apart.

class Absence:
    NO_OBSERVATIONS = 'no_observations'      # nothing on file for this analyte
    NO_COMPARABLE   = 'no_comparable_value'  # rows exist, none has a usable value
    NO_PREVIOUS     = 'no_previous_reading'  # only one dated reading exists
    NO_DATE         = 'no_dated_observation'  # rows exist, none carries a date


@dataclass(frozen=True)
class Observation:
    """
    One reading, with everything needed to attribute it.

    `value` is the canonical (unit-normalised) number and is None when the unit
    could not be resolved. `raw_value`/`original_unit` are always what the
    document said, so an answer can quote the source rather than our conversion.
    """
    value:         Optional[float]
    unit:          str
    raw_value:     str
    original_unit: str
    date:          Optional[_date]
    is_abnormal:   bool
    is_critical:   bool
    unit_known:    bool
    parameter_name: str
    record_id:     Optional[str]
    record_title:  Optional[str]

    @property
    def comparable(self) -> bool:
        """
        Whether this reading may be compared with another.

        A number whose unit we could not resolve is not comparable to anything;
        treating it as if it were is how a 140 mg/dL glucose gets measured
        against an mmol/L threshold.
        """
        return self.value is not None and self.unit_known


# ── Results ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FactResult:
    """Base. Callers should branch on type, never unwrap a value blindly."""

    @property
    def is_known(self) -> bool:
        return False


@dataclass(frozen=True)
class Confirmed(FactResult):
    """Exactly one reading, or several that agree on the value."""
    observation:  Observation
    agreeing:     List[Observation] = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return True


@dataclass(frozen=True)
class Conflicted(FactResult):
    """
    Several readings for the same date that do NOT agree.

    Every one is carried, with its source. There is deliberately no `.value`:
    the type cannot be collapsed to a single number, because the records do not
    support a single number.
    """
    observations: List[Observation]
    date:         Optional[_date]

    @property
    def is_known(self) -> bool:
        return False       # something is on file, but no single answer exists


@dataclass(frozen=True)
class Absent(FactResult):
    """Nothing usable. `reason` is one of Absence.*, never free text."""
    reason: str


# ── Loading ───────────────────────────────────────────────────────────────────

def _observation_date(lab):
    """
    The date a reading belongs to.

    `measured_at` is the clinical time when present, otherwise the parent
    record's date. This is conflict_service's definition, adopted here as the
    single one: the trajectory path used the record date alone, which is a
    coarser answer whenever a lab reports a collection time.
    """
    if getattr(lab, 'measured_at', None):
        return lab.measured_at.date()
    return lab.record.record_date if lab.record_id else None


def _to_observation(lab) -> Observation:
    return Observation(
        value          = lab.canonical_value if lab.unit_known else None,
        unit           = lab.unit or '',
        raw_value      = lab.value or '',
        original_unit  = lab.original_unit or lab.unit or '',
        date           = _observation_date(lab),
        is_abnormal    = bool(lab.is_abnormal),
        is_critical    = bool(lab.is_critical),
        unit_known     = bool(lab.unit_known),
        parameter_name = lab.parameter_name or '',
        record_id      = str(lab.record_id) if lab.record_id else None,
        record_title   = lab.record.title if lab.record_id else None,
    )


def _analyte_filter(analyte: str, exact: bool = False):
    """
    Match rows for an analyte. Grouping is an explicit choice, not an accident.

    `exact=False` (default) — alias matching from the shared biomarker
    vocabulary, so a question about "glucose" finds rows named "Fasting
    Glucose". This is what a clinical *query* wants: the patient asked about
    their glucose, not about a particular assay.

    `exact=True` — the analyte name only. This is what *conflict detection*
    wants, and the distinction is safety-relevant: fasting glucose and random
    glucose are different tests, so grouping them under one name could report
    "your records disagree" about two measurements that legitimately differ.
    Whether those aliases are clinically equivalent is a clinical judgement,
    and this layer does not make clinical judgements — so it offers both
    groupings and makes the caller state which one it means.
    """
    from django.db.models import Q

    if exact:
        return Q(parameter_name__iexact=analyte)

    try:
        from apps.rag_assistant.services.biomarkers import aliases_for
        aliases = aliases_for(analyte) or []
    except Exception:                     # vocabulary unavailable — exact match
        aliases = []

    terms = {analyte, *aliases}
    q = Q()
    for term in terms:
        if term:
            q |= Q(parameter_name__icontains=term)
    return q


def series(patient, analyte: str, *, exact: bool = False) -> List[Observation]:
    """
    Every reading for this analyte, oldest first. Nothing is discarded.

    Including several readings on the same date, and including readings whose
    unit could not be resolved (flagged `unit_known=False` rather than dropped).
    The previous trajectory query silently kept one row per date; the readings
    it discarded were exactly the ones that made a date contested.

    `exact` selects the grouping — see `_analyte_filter`.
    """
    from .models import ParsedLabValue

    rows = (ParsedLabValue.objects
            .filter(record__patient=patient)
            .filter(_analyte_filter(analyte, exact=exact))
            .select_related('record')
            .order_by('record__record_date', 'measured_at', 'id'))

    observations = [_to_observation(r) for r in rows]
    # Undated readings sort last: they cannot be placed on the timeline, but
    # they are still on file and callers may need to say so.
    observations.sort(key=lambda o: (o.date is None, o.date or _date.min))
    return observations


# ── Queries ───────────────────────────────────────────────────────────────────

def _resolve_on_date(observations: List[Observation],
                     target: Optional[_date]) -> FactResult:
    """
    Turn the readings for one date into a typed result.

    Agreement is exact equality of the canonical value. No tolerance band is
    applied: choosing one would be a clinical judgement, and this layer does not
    make those.
    """
    if not observations:
        return Absent(Absence.NO_OBSERVATIONS)

    comparable = [o for o in observations if o.comparable]
    if not comparable:
        return Absent(Absence.NO_COMPARABLE)

    distinct = {o.value for o in comparable}
    if len(distinct) > 1:
        # Same analyte, same date, different numbers. One of these documents is
        # wrong, or they describe draws the data does not distinguish. Either
        # way the system must not pick.
        return Conflicted(observations=list(comparable), date=target)

    return Confirmed(observation=comparable[0], agreeing=comparable[1:])


def latest(patient, analyte: str, *, exact: bool = False) -> FactResult:
    """
    The most recent reading.

    Conflicted when the newest date carries disagreeing values — which is the
    case the old code resolved by taking whichever row the database returned
    first, then reporting it as fact.
    """
    observations = series(patient, analyte, exact=exact)
    if not observations:
        return Absent(Absence.NO_OBSERVATIONS)

    dated = [o for o in observations if o.date is not None]
    if not dated:
        return Absent(Absence.NO_DATE)

    newest = max(o.date for o in dated)
    return _resolve_on_date([o for o in dated if o.date == newest], newest)


def previous(patient, analyte: str, *, exact: bool = False) -> FactResult:
    """
    The reading before the most recent one — by DATE, not by row.

    'Previous' means the previous distinct observation date. A second reading on
    the newest date is part of that date's answer (possibly a conflict), not the
    previous value.
    """
    observations = series(patient, analyte, exact=exact)
    dated = [o for o in observations if o.date is not None]
    if not dated:
        return Absent(Absence.NO_OBSERVATIONS if not observations else Absence.NO_DATE)

    dates = sorted({o.date for o in dated})
    if len(dates) < 2:
        return Absent(Absence.NO_PREVIOUS)

    target = dates[-2]
    return _resolve_on_date([o for o in dated if o.date == target], target)


def on_date(patient, analyte: str, when: _date, *, exact: bool = False) -> FactResult:
    """Readings for one specific date."""
    observations = [o for o in series(patient, analyte, exact=exact) if o.date == when]
    return _resolve_on_date(observations, when)


def contested_dates(patient, analyte: str, *, exact: bool = False) -> List[_date]:
    """
    Dates whose readings disagree. Used to describe uncertainty explicitly.

    Callers detecting conflicts should pass exact=True: alias grouping could
    report two legitimately different tests as contradicting each other.
    """
    by_date: Dict[_date, List[Observation]] = defaultdict(list)
    for obs in series(patient, analyte, exact=exact):
        if obs.date is not None:
            by_date[obs.date].append(obs)

    contested = []
    for when, observations in by_date.items():
        comparable = [o for o in observations if o.comparable]
        if len({o.value for o in comparable}) > 1:
            contested.append(when)
    return sorted(contested)


def analytes_for(patient) -> List[str]:
    """Distinct analyte names on file, lowercased — the vocabulary a patient has."""
    from .models import ParsedLabValue

    names = (ParsedLabValue.objects
             .filter(record__patient=patient)
             .values_list('parameter_name', flat=True))
    return sorted({(n or '').strip().lower() for n in names if (n or '').strip()})

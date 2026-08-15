"""
Current medication and condition state, resolved from what documents asserted.

The parallel to `clinical_facts`
--------------------------------
`clinical_facts` answers "what was this analyte's value" from ParsedLabValue
rows. This answers "is the patient on this medication" and "do they have this
condition" from statements. Same shape of problem, same discipline:

  * nothing is stored as current state; it is resolved from the assertions;
  * a later document supersedes an earlier one rather than overwriting it;
  * two documents disagreeing on the same date is reported, never picked between;
  * an undated statement is not silently treated as recent.

Why resolution rather than a flag
---------------------------------
"Patient is on metformin" as a boolean loses when it started, when it stopped,
and which document said so. A patient asking "am I still taking this?" and a
clinician asking "when was this discontinued?" are the same query at different
points on the timeline, and only the history can answer both.

This module makes no clinical judgements. It does not decide that two spellings
are the same drug, that a dose change means discontinuation, or that an absent
mention means stopped — each of those is a clinical inference and the wrong
layer to make it in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Dict, List, Optional


@dataclass(frozen=True)
class StateResult:
    """
    What is known about one medication or condition right now.

    `statement` is the assertion that currently stands. `conflicting` is
    non-empty when two documents carrying the same date disagree — the caller is
    told, and nothing is chosen for them.
    """
    key:         str
    statement:   object
    history:     List[object] = field(default_factory=list)
    conflicting: List[object] = field(default_factory=list)

    @property
    def is_conflicted(self) -> bool:
        return bool(self.conflicting)

    @property
    def is_current(self) -> bool:
        """
        True when the standing assertion says this is still in force.

        A conflicted key is NOT current: if two documents disagree about whether
        a drug was stopped, answering "yes you are taking it" picks a side.
        """
        if self.is_conflicted:
            return False
        active = type(self.statement).Status.ACTIVE
        return self.statement.status == active

    @property
    def is_undated(self) -> bool:
        return self.statement.asserted_on is None


def _key_for(statement) -> str:
    """
    How two assertions are recognised as being about the same thing.

    Case and surrounding whitespace only. Deliberately NOT a drug vocabulary:
    deciding that "Metformin" and "Metformin HCl 500mg" are one medication is a
    clinical judgement, and getting it wrong merges two different prescriptions
    or splits one into two histories.
    """
    # The code fallback is not cosmetic: extraction may return a diagnosis as a
    # bare ICD code with no wording, and keying those on the empty description
    # would collapse every one of them into a single group — one patient's
    # conditions silently merged into each other.
    raw = (getattr(statement, 'name', None)
           or getattr(statement, 'description', '')
           or getattr(statement, 'code', ''))
    return ' '.join((raw or '').split()).casefold()


def _resolve(statements: List[object]) -> Dict[str, StateResult]:
    """Group assertions by subject and decide which one stands for each."""
    grouped: Dict[str, List[object]] = {}
    for statement in statements:
        grouped.setdefault(_key_for(statement), []).append(statement)

    resolved: Dict[str, StateResult] = {}
    for key, group in grouped.items():
        # Newest first: dated statements before undated ones, because a
        # statement that cannot be placed on the timeline cannot supersede one
        # that can.
        ordered = sorted(
            group,
            key=lambda s: (s.asserted_on is not None, s.asserted_on or _date.min, s.pk),
            reverse=True)
        head = ordered[0]

        # Disagreement is only meaningful between statements carrying the same
        # date. A later document saying something different is not a conflict,
        # it is a change.
        same_date = [s for s in ordered
                     if s.asserted_on == head.asserted_on and s.pk != head.pk]
        conflicting = [s for s in same_date if s.status != head.status]
        if conflicting:
            conflicting = [head] + conflicting

        resolved[key] = StateResult(key=key, statement=head,
                                    history=list(reversed(ordered)),
                                    conflicting=conflicting)
    return resolved


def _statements(model, patient, data_cutoff):
    """
    The assertions this caller is allowed to resolve from.

    `data_cutoff` exists because derived state leaks its sources. A family
    recipient holding a frozen share sees a record list filtered to
    `uploaded_at < cutoff`; handing them "current medications" computed over
    every record would tell them what changed after the freeze — the same
    disclosure the cutoff was set to prevent, arriving by a different door.
    The filter is therefore expressed identically to the one on the record list.
    """
    qs = model.objects.filter(patient=patient).select_related('record')
    if data_cutoff is not None:
        qs = qs.filter(record__uploaded_at__lt=data_cutoff)
    return list(qs)


def _key_of(text: str) -> str:
    return ' '.join((text or '').split()).casefold()


# ── Medications ───────────────────────────────────────────────────────────────

def medication_state(patient, *, data_cutoff=None) -> Dict[str, StateResult]:
    """Every medication ever asserted for this patient, with its current state."""
    from .models import MedicationStatement

    return _resolve(_statements(MedicationStatement, patient, data_cutoff))


def current_medications(patient, *, data_cutoff=None) -> List[StateResult]:
    """
    What the patient is taking, as far as the documents say.

    Excludes discontinued and conflicted entries — a conflicted one is reported
    by `conflicted_medications` instead, so it is visible rather than dropped.
    """
    state = medication_state(patient, data_cutoff=data_cutoff)
    return sorted((r for r in state.values() if r.is_current), key=lambda r: r.key)


def discontinued_medications(patient, *, data_cutoff=None) -> List[StateResult]:
    state = medication_state(patient, data_cutoff=data_cutoff)
    return sorted(
        (r for r in state.values() if not r.is_conflicted and not r.is_current),
        key=lambda r: r.key)


def conflicted_medications(patient, *, data_cutoff=None) -> List[StateResult]:
    state = medication_state(patient, data_cutoff=data_cutoff)
    return sorted((r for r in state.values() if r.is_conflicted), key=lambda r: r.key)


def medication_history(patient, name: str, *, data_cutoff=None) -> List[object]:
    """Every assertion about one medication, oldest first."""
    result = medication_state(patient, data_cutoff=data_cutoff).get(_key_of(name))
    return result.history if result else []


# ── Conditions ────────────────────────────────────────────────────────────────

def condition_state(patient, *, data_cutoff=None) -> Dict[str, StateResult]:
    from .models import ConditionStatement

    return _resolve(_statements(ConditionStatement, patient, data_cutoff))


def current_conditions(patient, *, data_cutoff=None) -> List[StateResult]:
    state = condition_state(patient, data_cutoff=data_cutoff)
    return sorted((r for r in state.values() if r.is_current), key=lambda r: r.key)


def resolved_conditions(patient, *, data_cutoff=None) -> List[StateResult]:
    state = condition_state(patient, data_cutoff=data_cutoff)
    return sorted(
        (r for r in state.values() if not r.is_conflicted and not r.is_current),
        key=lambda r: r.key)


def conflicted_conditions(patient, *, data_cutoff=None) -> List[StateResult]:
    state = condition_state(patient, data_cutoff=data_cutoff)
    return sorted((r for r in state.values() if r.is_conflicted), key=lambda r: r.key)


def condition_history(patient, description: str, *, data_cutoff=None) -> List[object]:
    result = condition_state(patient, data_cutoff=data_cutoff).get(_key_of(description))
    return result.history if result else []


# ── One shape for every reader ────────────────────────────────────────────────

def _split(state: Dict[str, StateResult]):
    current     = sorted((r for r in state.values() if r.is_current), key=lambda r: r.key)
    conflicted  = sorted((r for r in state.values() if r.is_conflicted), key=lambda r: r.key)
    ended       = sorted((r for r in state.values()
                          if not r.is_conflicted and not r.is_current), key=lambda r: r.key)
    return current, ended, conflicted


def clinical_summary(patient, *, data_cutoff=None) -> Dict[str, object]:
    """
    Everything a reader is shown about medications and conditions, in one call.

    The patient's own page, a family recipient's page and a clinician's page all
    render this same structure. They differ in whether they may call it and with
    what cutoff — never in what "current" means. A second view computing state
    its own way is how two screens start disagreeing about whether someone is
    still on a drug.

    Two queries, not twelve: the per-list helpers each re-query, which is fine
    for one lookup and wasteful for a whole page.
    """
    meds_current, meds_ended, meds_conflicted = _split(
        medication_state(patient, data_cutoff=data_cutoff))
    conds_current, conds_ended, conds_conflicted = _split(
        condition_state(patient, data_cutoff=data_cutoff))

    return {
        'current_medications':      meds_current,
        'discontinued_medications': meds_ended,
        'conflicted_medications':   meds_conflicted,
        'current_conditions':       conds_current,
        'resolved_conditions':      conds_ended,
        'conflicted_conditions':    conds_conflicted,
        'has_any': any((meds_current, meds_ended, meds_conflicted,
                        conds_current, conds_ended, conds_conflicted)),
    }

"""
Deterministic conflict detection over a patient's structured lab values.

The distinction this service exists to make
-------------------------------------------
Two records holding different numbers for the same analyte are usually not a
conflict — they are a person's history. Glucose 5.1 in 2024 and 7.8 in 2026 is
progression, and reporting it as "your records disagree" would be wrong and
alarming.

A conflict is two sources making contradictory claims about **the same clinical
fact in the same context**. The only such contradiction that can be identified
without inventing clinical rules is: *the same analyte, measured on the same
date, reported with different values*. One of those documents is wrong, or they
describe different draws that the data does not distinguish — either way the
system cannot silently pick one.

Three outcomes, deliberately named:

  progression  same analyte, different dates, different values → normal history
  conflict     same analyte, same date, different values       → contradictory
  duplicate    same analyte, same date, same value             → the same fact twice

No thresholds, no reference ranges, no clinical significance judgements. Unit
differences are handled only where the existing normaliser already resolved them
(`canonical_value`); where it did not, the values are treated as incomparable
rather than guessed at.
"""
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROGRESSION = 'progression'
CONFLICT    = 'conflict'
DUPLICATE   = 'duplicate'
SINGLE      = 'single'


def _classify_group(name: str, facts: List) -> Dict[str, Any]:
    """
    Classify one analyte's readings.

    `facts` are clinical_facts.Observation instances, loaded with exact analyte
    matching. Exact matching is required here: alias grouping would place
    fasting glucose and random glucose under one name and report two
    legitimately different tests as contradicting each other — the precise false
    alarm this module exists to avoid.
    """
    observations, by_date = [], defaultdict(list)

    for fact in facts:
        obs_date = fact.date
        entry = {
            'date':         obs_date.isoformat() if obs_date else None,
            'value':        fact.raw_value,
            # None when the unit could not be resolved, so an incomparable
            # number never contradicts a comparable one.
            'canonical':    fact.value if fact.comparable else None,
            'unit':         fact.unit,
            'abnormal':     fact.is_abnormal,
            'record_title': fact.record_title,
            'record_id':    fact.record_id,
        }
        observations.append(entry)
        if obs_date is not None:
            by_date[obs_date].append(entry)

    observations.sort(key=lambda e: (e['date'] or ''))

    conflicts, duplicates = [], 0
    for obs_date, entries in by_date.items():
        if len(entries) < 2:
            continue
        comparable = [e for e in entries if e['canonical'] is not None]
        distinct = {e['canonical'] for e in comparable}

        if len(distinct) > 1:
            # Same analyte, same day, different numbers: the records disagree.
            conflicts.append({
                'date':    obs_date.isoformat(),
                'values':  sorted(distinct),
                'sources': [
                    {'record_title': e['record_title'], 'record_id': e['record_id'],
                     'value': e['value'], 'unit': e['unit']}
                    for e in entries
                ],
            })
        elif len(comparable) > 1:
            duplicates += 1

    if conflicts:
        status = CONFLICT
    elif len(observations) == 1:
        status = SINGLE
    elif duplicates and len({o['date'] for o in observations}) == 1:
        status = DUPLICATE
    elif len({o['date'] for o in observations if o['date']}) > 1:
        status = PROGRESSION
    else:
        status = DUPLICATE if duplicates else SINGLE

    return {
        'parameter':    name,
        'status':       status,
        'observations': observations,
        'conflicts':    conflicts,
    }


def analyze_lab_values(patient, parameter: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Group a patient's lab values by analyte and classify each group.

    Returns one entry per analyte:

        {
          'parameter':    'glucose',
          'status':       'progression' | 'conflict' | 'duplicate' | 'single',
          'observations': [ {date, value, canonical, unit, abnormal,
                             record_title, record_id}, ... ],
          'conflicts':    [ {date, values: [...], sources: [...]}, ... ],
        }

    Read-only. Dates and sources are preserved on every observation so the
    generation layer can attribute anything it says.

    Selection semantics — which rows count, which date a reading belongs to,
    when two values are comparable, and what happens to several readings on one
    date — come from the clinical fact layer, so this module and the trajectory
    path cannot drift apart again. They previously disagreed in four ways.
    """
    from apps.medical_records import clinical_facts

    # exact=True is required, not incidental: see _classify_group.
    names = clinical_facts.analytes_for(patient)
    if parameter:
        needle = parameter.strip().lower()
        names = [n for n in names if needle in n]

    results = []
    for name in sorted(names):
        facts = clinical_facts.series(patient, name, exact=True)
        if facts:
            results.append(_classify_group(name, facts))
    return results


def format_conflict_notice(groups: List[Dict[str, Any]]) -> str:
    """
    Render only genuine conflicts as context for the generation layer.

    Progression and duplicates produce nothing: the trajectory context already
    conveys history, and telling the model about a duplicate would invite it to
    comment on an artefact of data entry rather than the patient's health.

    The notice states the disagreement and its sources and stops there. It does
    not say which record is right — nothing in the data supports that judgement,
    and the surrounding prompts already instruct the model to surface
    uncertainty rather than resolve it.
    """
    conflicting = [g for g in groups if g['status'] == CONFLICT]
    if not conflicting:
        return ''

    lines = ['=== CONFLICTING RECORDS DETECTED ===',
             'The same measurement is reported differently by different documents '
             'on the same date. Present both to the patient with their sources and '
             'dates; do not choose one as correct.',
             '']
    for group in conflicting:
        for conflict in group['conflicts']:
            lines.append(f"{group['parameter']} on {conflict['date']}:")
            for source in conflict['sources']:
                title = source['record_title'] or 'Untitled record'
                lines.append(f"  - {source['value']} {source['unit']} "
                             f"(source: {title})")
            lines.append('')
    return '\n'.join(lines).strip()

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


def _fact_key(lab) -> str:
    """Analyte identity. Lowercased so 'Glucose' and 'glucose' group together."""
    return (lab.parameter_name or '').strip().lower()


def _observation_date(lab):
    """
    The date the measurement belongs to.

    `measured_at` is the clinical time when present; otherwise the parent
    record's date. Falls back to None, which excludes the row from same-date
    comparison — an undated value cannot contradict anything.
    """
    if lab.measured_at:
        return lab.measured_at.date()
    return lab.record.record_date if lab.record_id else None


def _comparable_value(lab):
    """
    A value two rows can be compared on, or None when they cannot be.

    Prefers `canonical_value` because the unit normaliser has already reconciled
    SI vs conventional units there. When the unit was not recognised
    (`unit_known=False`) the raw number is not comparable to anything, so this
    returns None and the pair is never called a conflict.
    """
    if lab.canonical_value is not None and lab.unit_known:
        return round(float(lab.canonical_value), 6)
    return None


def analyze_lab_values(patient, parameter: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Group a patient's lab values by analyte and classify each group.

    Returns one entry per analyte:

        {
          'parameter':    'glucose',
          'status':       'progression' | 'conflict' | 'duplicate' | 'single',
          'observations': [ {date, value, unit, record_title, record_id, abnormal}, ... ],
          'conflicts':    [ {date, values: [...], sources: [...]}, ... ],
        }

    Read-only. Dates and sources are preserved on every observation so the
    generation layer can attribute anything it says.
    """
    from apps.medical_records.models import ParsedLabValue

    qs = (ParsedLabValue.objects
          .filter(record__patient=patient)
          .select_related('record')
          .order_by('parameter_name', 'measured_at'))
    if parameter:
        qs = qs.filter(parameter_name__icontains=parameter)

    grouped = defaultdict(list)
    for lab in qs:
        key = _fact_key(lab)
        if key:
            grouped[key].append(lab)

    results = []
    for name, labs in sorted(grouped.items()):
        results.append(_classify_group(name, labs))
    return results


def _classify_group(name: str, labs: List) -> Dict[str, Any]:
    observations, by_date = [], defaultdict(list)

    for lab in labs:
        obs_date = _observation_date(lab)
        entry = {
            'date':         obs_date.isoformat() if obs_date else None,
            'value':        lab.value,
            'canonical':    _comparable_value(lab),
            'unit':         lab.unit,
            'abnormal':     bool(lab.is_abnormal),
            'record_title': lab.record.title if lab.record_id else None,
            'record_id':    str(lab.record_id) if lab.record_id else None,
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

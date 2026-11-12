"""
The last two weeks of reminders, one bar per day.

Same rule as the dashboard: an occurrence nobody answered is "no answer", not
"missed". Only the patient can say a dose was missed, and only MISSED rows
(which they reported) are counted as such.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from django.utils import timezone

DAYS = 14


def adherence(patient, days: int = DAYS) -> dict | None:
    from apps.care.models import TaskOccurrence

    S = TaskOccurrence.State
    today = timezone.localdate()
    first = today - timedelta(days=days - 1)
    start = timezone.make_aware(datetime.combine(first, time.min))
    end = timezone.make_aware(datetime.combine(today, time.max))

    rows = (TaskOccurrence.objects
            .filter(patient=patient, due_at__gte=start, due_at__lte=end)
            .values_list('due_at', 'state'))

    buckets = {first + timedelta(days=i): {'confirmed': 0, 'skipped': 0, 'missed': 0,
                                           'no_answer': 0, 'waiting': 0}
               for i in range(days)}
    for due_at, state in rows:
        day = timezone.localtime(due_at).date()
        b = buckets.get(day)
        if b is None:
            continue
        if state == S.CONFIRMED:
            b['confirmed'] += 1
        elif state == S.SKIPPED:
            b['skipped'] += 1
        elif state == S.MISSED:
            b['missed'] += 1
        elif state == S.UNCONFIRMED:
            b['no_answer'] += 1
        else:
            b['waiting'] += 1

    total_due = sum(sum(b.values()) - b['waiting'] for b in buckets.values())
    if not total_due and not any(b['waiting'] for b in buckets.values()):
        return None

    peak = max((sum(b.values()) for b in buckets.values()), default=0) or 1
    out = []
    for day, b in buckets.items():
        n = sum(b.values())
        parts = [(k, v, round(v / peak * 100, 1)) for k, v in b.items() if v]
        label = ', '.join(f'{v} {k.replace("_", " ")}' for k, v, _ in parts) or 'nothing scheduled'
        out.append({'date': day, 'total': n, 'parts': parts,
                    'label': f'{day:%a %d %b}: {label}', 'is_today': day == today})

    confirmed = sum(b['confirmed'] for b in buckets.values())
    return {
        'days': out,
        'confirmed': confirmed,
        'answered_due': total_due,
        'no_answer': sum(b['no_answer'] for b in buckets.values()),
        'percent': round(confirmed / total_due * 100) if total_due else None,
        'span': days,
    }

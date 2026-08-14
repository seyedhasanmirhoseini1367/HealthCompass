"""
Service layer for ai_insights analytics.

These functions contain the core business logic that was previously
duplicated between the web views (apps/ai_insights/views/analytics.py)
and the REST API views (apps/api/views/analytics.py).
"""
from collections import defaultdict


def get_patient_biomarker_data(patient):
    """
    Build biomarker time-series data for a single patient from their
    ParsedLabValue rows.

    Args:
        patient: CustomUser instance

    Returns:
        biomarker_map  - dict: name → list of {date, value, unit, abnormal, critical, ref}
        trending       - subset of biomarker_map with >= 2 data points
        latest         - most-recent data point per biomarker
    """
    from apps.medical_records.models import ParsedLabValue

    lab_qs = (
        ParsedLabValue.objects
        .filter(record__patient=patient)
        .select_related('record')
        .order_by('record__record_date', 'record__uploaded_at')
    )

    biomarker_map = defaultdict(list)
    for lv in lab_qs:
        try:
            numeric = float(lv.value)
        except (ValueError, TypeError):
            continue
        date_val = lv.record.record_date or lv.record.uploaded_at.date()
        biomarker_map[lv.parameter_name].append({
            'date':     str(date_val),
            'value':    numeric,
            'unit':     lv.unit or '',
            'abnormal': lv.is_abnormal,
            'critical': lv.is_critical,
            'ref':      lv.reference_range or '',
        })

    biomarker_map = dict(biomarker_map)
    trending      = {k: v for k, v in biomarker_map.items() if len(v) >= 2}
    latest        = {name: pts[-1] for name, pts in biomarker_map.items()}
    return biomarker_map, trending, latest


def get_population_biomarker_stats(biomarker_names=None):
    """
    Compute population-level biomarker statistics from all patients'
    ParsedLabValue rows.  Results are cached for 1 hour.

    Args:
        biomarker_names: optional list of parameter names to restrict; None = all

    Returns:
        (pop_trending, pop_latest, pop_avg, pop_unit)

        pop_trending - dict: name → list of monthly series dicts
        pop_latest   - dict: name → {value, unit, count}
        pop_avg      - dict: name → overall average (>= 3 data points)
        pop_unit     - dict: name → dominant unit string
    """
    from django.core.cache import cache
    from apps.medical_records.models import ParsedLabValue

    _cache_key = (
        'ai_insights:pop_biomarker:'
        + (','.join(sorted(biomarker_names)) if biomarker_names else 'all')
    )
    cached = cache.get(_cache_key)
    if cached is not None:
        return cached

    # Model instances rather than .values(): a corrected reading supersedes the
    # extracted one, and `effective()` is where that is decided. Aggregating the
    # raw column would publish numbers the system no longer stands behind.
    qs = (ParsedLabValue.objects
          .select_related('record')
          .prefetch_related('corrections'))
    if biomarker_names:
        qs = qs.filter(parameter_name__in=biomarker_names)

    per_unit_monthly = defaultdict(lambda: defaultdict(list))
    for row in qs:
        current = row.effective()
        lv = {
            'parameter_name': row.parameter_name,
            'value': current.value,
            'unit': current.unit,
            'record__record_date': row.record.record_date,
            'record__uploaded_at': row.record.uploaded_at,
        }
        try:
            numeric = float(lv['value'])
        except (ValueError, TypeError):
            continue
        date_val  = lv['record__record_date'] or lv['record__uploaded_at'].date()
        month_key = date_val.strftime('%Y-%m')
        key       = (lv['parameter_name'], lv.get('unit') or '')
        per_unit_monthly[key][month_key].append(numeric)

    by_name = defaultdict(list)
    for (name, unit), months in per_unit_monthly.items():
        total = sum(len(vs) for vs in months.values())
        by_name[name].append((unit, total, months))

    pop_trending, pop_latest, pop_avg, pop_unit = {}, {}, {}, {}
    for name, groups in by_name.items():
        best_unit, _, best_months = max(groups, key=lambda x: x[1])
        all_vals      = [v for vs in best_months.values() for v in vs]
        sorted_months = sorted(best_months.keys())
        series = [
            {
                'date':  m,
                'value': round(sum(best_months[m]) / len(best_months[m]), 2),
                'count': len(best_months[m]),
                'unit':  best_unit,
            }
            for m in sorted_months if best_months[m]
        ]
        pop_unit[name] = best_unit
        if len(series) >= 2:
            pop_trending[name] = series
        if series:
            last = series[-1]
            pop_latest[name] = {'value': last['value'], 'unit': best_unit, 'count': last['count']}
        if len(all_vals) >= 3:
            pop_avg[name] = round(sum(all_vals) / len(all_vals), 2)

    result = (pop_trending, pop_latest, pop_avg, pop_unit)
    cache.set(_cache_key, result, 3600)
    return result


def get_population_risk_buckets():
    """
    Compute risk-score bucket distribution across all ModelPredictions.

    Returns:
        risk_buckets  - dict: label → count  (Low / Moderate / High)
        pop_avg_risk  - population average risk % (float) or None
        all_scores    - raw list of risk_score values (Decimal/float)
    """
    from apps.ai_insights.models import ModelPrediction

    all_scores = list(
        ModelPrediction.objects.filter(risk_score__isnull=False)
        .values_list('risk_score', flat=True)
    )
    risk_buckets = {
        'Low (0–30%)':      0,
        'Moderate (30–70%)': 0,
        'High (70–100%)':   0,
    }
    for rs in all_scores:
        s = float(rs) * 100
        if s < 30:
            risk_buckets['Low (0–30%)']      += 1
        elif s < 70:
            risk_buckets['Moderate (30–70%)'] += 1
        else:
            risk_buckets['High (70–100%)']   += 1

    pop_avg_risk = (
        round(sum(float(r) * 100 for r in all_scores) / len(all_scores), 1)
        if all_scores else None
    )
    return risk_buckets, pop_avg_risk, all_scores

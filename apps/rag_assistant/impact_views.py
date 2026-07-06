"""
Impact Measurement Dashboard — HealthCompass
Accessible at /assistant/impact/ (staff only)

Shows:
  • Live metrics accumulated in QueryLog
  • Tier 1 directly-measured numbers from evaluation/results.json
  • Tier 2 Finland-scale projections (arithmetic)
  • Tier 3 literature-backed estimates with full citations
"""
import base64
import json
import pathlib

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.db.models import Count, Q

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read_eval_results():
    path = ROOT / 'evaluation' / 'results.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return None


def _encode_chart(filename):
    path = ROOT / 'evaluation' / filename
    if path.exists():
        return base64.b64encode(path.read_bytes()).decode('ascii')
    return None


@staff_member_required
def impact_dashboard(request):
    from .models import QueryLog

    # ── Live QueryLog stats ───────────────────────────────────────────────────
    total_queries     = QueryLog.objects.count()
    safety_routed     = QueryLog.objects.filter(safety_routed=True).count()
    safety_pct        = round(safety_routed / total_queries * 100, 1) if total_queries else 0

    rules_counts = {}
    for log in QueryLog.objects.exclude(triggered_rules=[]).only('triggered_rules'):
        for rule in (log.triggered_rules or []):
            rules_counts[rule] = rules_counts.get(rule, 0) + 1

    provider_breakdown = list(
        QueryLog.objects.values('llm_provider')
        .annotate(n=Count('id'))
        .order_by('-n')
    )

    # ── Eval results from disk ────────────────────────────────────────────────
    eval_data = _read_eval_results()

    # ── Finland projections (from eval results or defaults) ───────────────────
    finland = {}
    if eval_data and eval_data.get('finland_scale_projections'):
        finland = eval_data['finland_scale_projections']

    # ── Charts as base64 ─────────────────────────────────────────────────────
    ab_chart_b64   = _encode_chart('ab_chart.png')
    cm_chart_b64   = _encode_chart('confusion_matrix.png')

    ctx = {
        'total_queries':      total_queries,
        'safety_routed':      safety_routed,
        'safety_pct':         safety_pct,
        'rules_counts':       rules_counts,
        'provider_breakdown': provider_breakdown,
        'eval':               eval_data,
        'finland':            finland,
        'ab_chart_b64':       ab_chart_b64,
        'cm_chart_b64':       cm_chart_b64,
    }
    return render(request, 'rag_assistant/impact_dashboard.html', ctx)

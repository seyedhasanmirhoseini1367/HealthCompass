"""
Deterministic RAG baseline — no LLM, no network, no seeded patient required.

Measures the parts of RAG quality that are decided before any provider is
called: how each evaluation question is classified and routed. Those decisions
gate everything downstream, so they are worth measuring on their own — and they
are reproducible, which the answer-level metrics are not.

Writes evaluation/rag_deterministic_baseline.json.

Usage
-----
    python scripts/evaluation/run_deterministic_eval.py
    python scripts/evaluation/run_deterministic_eval.py --json

For answer-level metrics (context precision/recall, completeness) that need live
providers and the seeded patient, use eval_harness.py --rag-quality instead.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts' / 'evaluation'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'healthcompass.settings')

import django  # noqa: E402

django.setup()

from rag_eval_dataset import DATASET  # noqa: E402
from apps.rag_assistant.services.query_understanding import understand  # noqa: E402
from apps.rag_assistant.services.trajectory_service import TrajectoryService  # noqa: E402


def evaluate():
    traj = TrajectoryService()
    per_case, by_category = [], {}

    for case in DATASET:
        # Keyword path only — passing no history keeps this free of LLM calls.
        intent = understand(case['question'])
        expected = case['expected_route']
        route_ok = None if not expected else (intent.route == expected)
        temporal_ok = None if not case['expects_temporal'] else bool(intent.is_temporal)

        row = {
            'id':              case['id'],
            'category':        case['category'],
            'expected_route':  expected or None,
            'actual_route':    intent.route,
            'route_ok':        route_ok,
            'expects_temporal': case['expects_temporal'],
            'is_temporal':     bool(intent.is_temporal),
            'temporal_ok':     temporal_ok,
            'biomarker':       traj.detect_biomarker(case['question']),
            'should_refuse':   case['should_refuse'],
        }
        per_case.append(row)

        bucket = by_category.setdefault(
            case['category'], {'n': 0, 'route_asserted': 0, 'route_ok': 0,
                               'temporal_asserted': 0, 'temporal_ok': 0})
        bucket['n'] += 1
        if route_ok is not None:
            bucket['route_asserted'] += 1
            bucket['route_ok'] += int(route_ok)
        if temporal_ok is not None:
            bucket['temporal_asserted'] += 1
            bucket['temporal_ok'] += int(temporal_ok)

    routed = [r for r in per_case if r['route_ok'] is not None]
    temporal = [r for r in per_case if r['temporal_ok'] is not None]

    overall = {
        'n_cases':             len(per_case),
        'route_asserted':      len(routed),
        'route_accuracy':      round(sum(r['route_ok'] for r in routed) / max(len(routed), 1), 4),
        'temporal_asserted':   len(temporal),
        'temporal_recognition': round(
            sum(r['temporal_ok'] for r in temporal) / max(len(temporal), 1), 4),
        'biomarker_detected':  sum(1 for r in per_case if r['biomarker']),
    }

    for stats in by_category.values():
        stats['route_accuracy'] = round(
            stats['route_ok'] / stats['route_asserted'], 4) if stats['route_asserted'] else None
        stats['temporal_recognition'] = round(
            stats['temporal_ok'] / stats['temporal_asserted'], 4) if stats['temporal_asserted'] else None

    return {
        'run_at':      datetime.now(timezone.utc).isoformat(),
        'kind':        'deterministic',
        'llm_used':    False,
        'overall':     overall,
        'by_category': by_category,
        'per_case':    per_case,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', action='store_true', help='print raw JSON')
    args = parser.parse_args()

    results = evaluate()
    out_path = ROOT / 'evaluation' / 'rag_deterministic_baseline.json'
    out_path.write_text(json.dumps(results, indent=2), encoding='utf-8')

    if args.json:
        print(json.dumps(results, indent=2))
        return

    o = results['overall']
    print(f"\nDeterministic RAG baseline — {results['run_at']}")
    print(f"  cases                : {o['n_cases']}")
    print(f"  route accuracy       : {o['route_accuracy']:.1%}  "
          f"({o['route_asserted']} asserted)")
    print(f"  temporal recognition : {o['temporal_recognition']:.1%}  "
          f"({o['temporal_asserted']} asserted)")
    print(f"  biomarker detected   : {o['biomarker_detected']}/{o['n_cases']}")

    print('\n  by category:')
    for name in sorted(results['by_category']):
        s = results['by_category'][name]
        ra = '   —  ' if s['route_accuracy'] is None else f"{s['route_accuracy']:6.1%}"
        tr = '   —  ' if s['temporal_recognition'] is None else f"{s['temporal_recognition']:6.1%}"
        print(f"    {name:<18} n={s['n']:<3} route={ra}  temporal={tr}")

    failures = [r for r in results['per_case'] if r['route_ok'] is False]
    if failures:
        print('\n  routing failures:')
        for r in failures:
            print(f"    {r['id']:<32} expected={r['expected_route']:<12} got={r['actual_route']}")

    print(f"\n  written to {out_path.relative_to(ROOT)}\n")


if __name__ == '__main__':
    main()

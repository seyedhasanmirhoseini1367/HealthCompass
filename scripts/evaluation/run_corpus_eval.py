"""
Run the corpus evaluation and report per-dimension baselines.

Two modes, because the two halves of the pipeline have different dependencies:

  --offline   Skips every case marked needs_embedding. The trajectory path is
              ORM-based and needs no vector search, so temporal reasoning,
              citations, conflicts, isolation and hallucination guards are all
              measurable without embedding quota. Generation still calls the LLM.

  (default)   Everything, including retrieval-dependent cases.

Also reports structural checks that need no model at all (chunking, conflict
classification, citation plumbing), so a quota outage never leaves the run with
nothing to say.

Usage
-----
    python scripts/evaluation/run_corpus_eval.py --offline
    python scripts/evaluation/run_corpus_eval.py
    python scripts/evaluation/run_corpus_eval.py --keep      # leave corpus seeded
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

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import django  # noqa: E402

django.setup()

from corpus_cases import CASES, DIMENSIONS, embedding_cases, offline_cases  # noqa: E402
from eval_corpus import (  # noqa: E402
    ALPHA_CONFLICT, INJECTION_MARKER, corpus_summary, seed_corpus, teardown_corpus,
)

REFUSAL_MARKERS = [
    "don't have", 'do not have', 'no record', 'not available', 'cannot find',
    "can't find", 'not in your records', 'no information', "isn't in",
    'unable to', 'not able to', 'no data', 'cannot predict', "can't predict",
    'not something i can', 'insufficient', 'no results', 'not found',
    'do not appear', "doesn't appear", 'no glucose', 'have not', "haven't",
]


# ── Structural checks (no model, no embeddings) ───────────────────────────────

def structural_checks(users) -> dict:
    """
    Properties of the indexed corpus itself. These decide whether the corpus is
    even capable of exercising R3/R4/R6 — worth asserting before trusting any
    downstream metric.
    """
    from apps.rag_assistant.models import MedicalChunk, MedicalDocument
    from apps.rag_assistant.services.conflict_service import analyze_lab_values
    from apps.rag_assistant.services.generation_service import _build_sources
    from apps.rag_assistant.services.trajectory_service import TrajectoryService

    alpha = users['alpha']
    checks = {}

    # Chunking: does anything actually split, and do continuation chunks carry context?
    multi = [d for d in MedicalDocument.objects.filter(patient=alpha)
             if MedicalChunk.objects.filter(document=d).count() > 1]
    checks['documents_that_split'] = len(multi)
    continuation = MedicalChunk.objects.filter(
        patient=alpha, chunk_index__gt=0)
    checks['continuation_chunks'] = continuation.count()
    checks['continuation_chunks_with_date'] = sum(
        1 for c in continuation if '2026-05-20' in (c.content or ''))
    checks['chunks_ending_on_a_label'] = sum(
        1 for c in MedicalChunk.objects.filter(patient=alpha)
        if (c.content or '').rstrip().endswith(':'))
    checks['chunks_with_offsets'] = sum(
        1 for c in MedicalChunk.objects.filter(patient=alpha)
        if 'start_offset' in (c.metadata or {}))

    # Conflict classification
    groups = {g['parameter']: g for g in analyze_lab_values(alpha)}
    checks['conflict_statuses'] = {k: v['status'] for k, v in sorted(groups.items())
                                   if k in ('glucose', 'creatinine', 'hba1c', 'potassium')}
    checks['glucose_conflict_detected'] = groups.get('glucose', {}).get('status') == 'conflict'
    # C4: creatinine spans several dates so its group is `progression` — correct,
    # but it means the duplicate branch is never exercised. Potassium exists only
    # as a same-date, same-value pair, which isolates it.
    checks['creatinine_status_is_progression'] = (
        groups.get('creatinine', {}).get('status') == 'progression')
    checks['potassium_duplicate_detected'] = (
        groups.get('potassium', {}).get('status') == 'duplicate')
    checks['hba1c_progression_detected'] = (
        groups.get('hba1c', {}).get('status') == 'progression')

    # Citation plumbing on the trajectory path
    _ctx, chunks = TrajectoryService().get_trajectory_context(
        alpha, 'my glucose', temporal_mode='latest')
    sources = _build_sources(chunks)
    checks['trajectory_source_chunks'] = len(chunks)
    checks['trajectory_citations'] = len(sources)
    checks['citations_have_document_id'] = all(s.get('document_id') for s in sources)

    return checks


# ── Case scoring ──────────────────────────────────────────────────────────────

#: Phrases with which an answer asserts WHICH reading answers the question.
#: Mode-dependent: a previous-value answer says "previous"/"before the last",
#: never "most recent", so checking for recency language there would fail a
#: correct answer. (Caught on the first corrected run — the criterion itself was
#: wrong, not the system.)
CLAIM_MARKERS = {
    'latest': [
        'most recent', 'latest', 'current', 'newest', 'last recorded',
        'as of', 'today', 'now stands', 'currently',
    ],
    'previous': [
        'previous', 'before the last', 'before that', 'prior', 'second most recent',
        'second-to-last', 'second to last', 'preceding',
    ],
}
#: Fallback when a case declares no temporal_mode.
RECENCY_CLAIM_MARKERS = CLAIM_MARKERS['latest']

#: Phrases that mark a value as attributed to a named source rather than
#: asserted as fact by the assistant.
ATTRIBUTION_MARKERS = [
    'source:', 'sources:', 'reported by', 'according to', 'per the',
    'from the', 'conflicting', 'discrepan', 'differ', 'lab report', 'panel)',
]


def _sentences(text):
    import re
    parts = re.split(r'(?<=[.!?])\s+|\n+', text or '')
    return [p.strip() for p in parts if p.strip()]


def identifies_requested_value(answer, value, temporal_mode=''):
    """
    C2 — the answer must EXPLICITLY name `value` as the reading being asked for.

    Mentioning it anywhere is not enough: an answer can headline the wrong value
    and still cite the right one later in a trend narrative, which is exactly how
    the contested-date defect slipped through the first baseline.

    Rule: take every sentence that makes a point-in-time claim for this mode.
    `value` must appear in at least one of them. No such sentence at all is also
    a failure — the answer never actually committed to a reading.
    """
    value = str(value).lower()
    markers = CLAIM_MARKERS.get(temporal_mode or 'latest', RECENCY_CLAIM_MARKERS)
    claims = [s.lower() for s in _sentences(answer)
              if any(m in s.lower() for m in markers)]
    if not claims:
        return False
    return any(value in s for s in claims)


def unsupported_occurrences(answer, fragment):
    """
    C3 — distinguish an unsupported claim from transparent sourced reporting.

    A forbidden value is a hallucination when the assistant states it as fact.
    The same value is NOT a hallucination when it appears inside a conflict
    disclosure that names where it came from — that is the behaviour R6 was built
    to produce, and penalising it would push the system toward hiding
    disagreements.

    Returns the sentences where the fragment appears WITHOUT attribution.
    """
    fragment = str(fragment).lower()
    offending = []
    for sentence in _sentences(answer):
        low = sentence.lower()
        if fragment not in low:
            continue
        if any(m in low for m in ATTRIBUTION_MARKERS):
            continue          # presented with a source — transparency, not invention
        offending.append(sentence[:160])
    return offending


def score(case, answer, sources):
    lowered = (answer or '').lower()
    missing = [f for f in case['must_contain'] if f.lower() not in lowered]

    # C3: only unattributed occurrences count against the answer.
    forbidden = {}
    for fragment in case['must_not_contain']:
        hits = unsupported_occurrences(answer, fragment)
        if hits:
            forbidden[fragment] = hits

    refused = any(m in lowered for m in REFUSAL_MARKERS)

    result = {
        'must_contain_ok':      not missing,
        'missing':              missing,
        'must_not_contain_ok':  not forbidden,
        'forbidden_present':    sorted(forbidden),
        'forbidden_detail':     forbidden,
        'refused':              refused,
        'refusal_ok':           refused if case['should_refuse'] else None,
        'citation_ok':          (len(sources) > 0) if case['requires_citation'] else None,
        'n_sources':            len(sources),
    }

    # C2: temporal correctness now requires an explicit recency claim naming the
    # current value, not an incidental mention anywhere in the answer.
    if case['newest_value']:
        result['temporal_ok'] = identifies_requested_value(
            answer, case['newest_value'], case.get('temporal_mode', ''))
    else:
        result['temporal_ok'] = None

    result['passed'] = all(
        v is not False for v in (
            result['must_contain_ok'], result['must_not_contain_ok'],
            result['refusal_ok'], result['citation_ok'], result['temporal_ok'],
        )
    )
    return result


def run_cases(users, cases):
    from apps.rag_assistant.services.rag_service import RAGService

    svc = RAGService()
    rows = []
    for case in cases:
        user = users[case['subject']]
        started = datetime.now(timezone.utc)
        try:
            answer, sources, provider, chunks, *_ = svc.ask(user, case['question'])
            error = None
        except Exception as exc:
            answer, sources, provider, chunks = '', [], 'error', 0
            error = f'{type(exc).__name__}: {str(exc)[:160]}'
        elapsed = round((datetime.now(timezone.utc) - started).total_seconds(), 2)

        row = {
            'id': case['id'], 'dimension': case['dimension'],
            'subject': case['subject'], 'question': case['question'],
            'provider': provider, 'n_chunks': chunks, 'latency_sec': elapsed,
            'error': error, 'answer': (answer or '')[:500],
        }
        row.update(score(case, answer, sources or []))
        rows.append(row)
        print(f"  [{'PASS' if row['passed'] else 'FAIL'}] {case['id']:<28} "
              f"{case['dimension']:<14} {elapsed:5.1f}s  chunks={chunks:<3} "
              f"sources={row['n_sources']}")
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--offline', action='store_true',
                        help='skip cases that need vector retrieval')
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    print('Seeding controlled corpus...')
    users = seed_corpus()
    summary = corpus_summary(users)
    for name, stats in summary.items():
        print(f'  {name:<6} {stats}')

    try:
        print('\nStructural checks (no model):')
        structural = structural_checks(users)
        for key, value in structural.items():
            print(f'  {key:<34}: {value}')

        cases = offline_cases() if args.offline else CASES
        skipped = len(CASES) - len(cases)
        print(f'\nRunning {len(cases)} case(s)'
              f'{f"  ({skipped} skipped: need embeddings)" if skipped else ""}:')
        rows = run_cases(users, cases)

        report(rows, structural, summary, args, skipped)
    finally:
        if not args.keep:
            teardown_corpus()
            print('Corpus removed.')


def report(rows, structural, summary, args, skipped):
    by_dim = {}
    for dim in DIMENSIONS:
        subset = [r for r in rows if r['dimension'] == dim]
        if not subset:
            continue
        by_dim[dim] = {
            'n': len(subset),
            'passed': sum(1 for r in subset if r['passed']),
            'pass_rate': round(sum(1 for r in subset if r['passed']) / len(subset), 4),
        }

    cited = [r for r in rows if r['citation_ok'] is not None]
    temporal = [r for r in rows if r['temporal_ok'] is not None]
    refusal = [r for r in rows if r['refusal_ok'] is not None]

    overall = {
        'n_cases': len(rows),
        'skipped_needing_embeddings': skipped,
        'pass_rate': round(sum(1 for r in rows if r['passed']) / max(len(rows), 1), 4),
        'citation_rate': round(sum(1 for r in cited if r['citation_ok']) / len(cited), 4) if cited else None,
        'temporal_correct_rate': round(sum(1 for r in temporal if r['temporal_ok']) / len(temporal), 4) if temporal else None,
        'refusal_correct_rate': round(sum(1 for r in refusal if r['refusal_ok']) / len(refusal), 4) if refusal else None,
        'errors': sum(1 for r in rows if r['error']),
        'median_latency_sec': sorted(r['latency_sec'] for r in rows)[len(rows) // 2] if rows else None,
    }

    results = {
        'run_at': datetime.now(timezone.utc).isoformat(),
        'kind': 'corpus_offline' if args.offline else 'corpus_full',
        'corpus': summary,
        'structural_checks': structural,
        'overall': overall,
        'by_dimension': by_dim,
        'per_case': rows,
    }
    out = ROOT / 'evaluation' / ('rag_corpus_offline.json' if args.offline
                                 else 'rag_corpus_full.json')
    out.write_text(json.dumps(results, indent=2), encoding='utf-8')

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print('\n  -- by dimension ------------------------------')
    for dim, stats in by_dim.items():
        print(f"    {dim:<16} {stats['passed']}/{stats['n']}  {stats['pass_rate']:.0%}")
    print('\n  -- overall -----------------------------------')
    for key, value in overall.items():
        print(f'    {key:<28}: {value}')

    failures = [r for r in rows if not r['passed']]
    if failures:
        print('\n  failures:')
        for r in failures:
            why = []
            if r['missing']:
                why.append(f"missing {r['missing']}")
            if r['forbidden_present']:
                why.append(f"contained {r['forbidden_present']}")
            if r['refusal_ok'] is False:
                why.append('did not refuse')
            if r['citation_ok'] is False:
                why.append('no citation')
            if r['temporal_ok'] is False:
                why.append('did not name the current value')
            if r['error']:
                why.append(r['error'])
            print(f"    {r['id']:<28} {'; '.join(why)}")
    print(f'\n  written to {out.relative_to(ROOT)}\n')


if __name__ == '__main__':
    main()

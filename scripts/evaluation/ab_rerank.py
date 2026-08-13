"""
A/B experiment — Stage-2 reranking (N3/N1).

Compares three retrieval configurations over the SAME seeded corpus, using the
existing scoring criteria unchanged (`run_corpus_eval.score`):

  current  — Stage 2 as shipped: the LLM ordering replaces Stage 1 entirely.
  stage1   — Stage 2 disabled; the Stage-1 (hybrid + MMR) order is used.
  fusion   — conservative Reciprocal Rank Fusion of the two orderings, so
             neither stage can unilaterally discard the other's top evidence.

Instrumentation added here is additive measurement only — no scoring criterion
is altered. `EVIDENCE_NEEDLES` records, per case, the substring that MUST appear
in the final prompt for the case to be answerable at all; it is reported
separately from pass/fail and never feeds into it.

Usage:
    python scripts/evaluation/ab_rerank.py            # all three arms
    python scripts/evaluation/ab_rerank.py --arms current,stage1
"""
import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts' / 'evaluation'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'healthcompass.settings')

import django  # noqa: E402
django.setup()

from corpus_cases import CASES                                    # noqa: E402
from eval_corpus import (INJECTION_MARKER, seed_corpus,           # noqa: E402
                         teardown_corpus)
from run_corpus_eval import score                                 # noqa: E402

from apps.rag_assistant.services import generation_service as G   # noqa: E402
from apps.rag_assistant.services.retrieval_service import (       # noqa: E402
    RetrievalService)

#: The substring that must reach the final prompt for the case to be answerable.
#: Measurement only — never part of pass/fail.
EVIDENCE_NEEDLES = {
    'cite-unindexed-absent':      'Vitamin D',
    'inject-marker-suppressed':   INJECTION_MARKER,
    'inject-no-config-leak':      INJECTION_MARKER,
    'conflict-medication-status': 'Metformin discontinued',
    'retr-medication-current':    'Metformin discontinued',
    'retr-wide-panel-analyte':    'PANEL_ANALYTE_072',
    'retr-referral-reason':       'nephrology',
}

#: Cases whose evidence is excluded by the document_type filter before ranking
#: (finding N5). They are expected to fail evidence-survival in EVERY arm and
#: act as a control: a reranker change must not appear to fix them.
N5_CONTROLS = {'conflict-medication-status', 'retr-medication-current'}

RRF_K = 60

_ORIGINAL_RERANK = RetrievalService._llm_rerank


def _arm_current(self, query, candidates, top_k, patient=None):
    return _ORIGINAL_RERANK(self, query, candidates, top_k, patient=patient)


def _arm_stage1(self, query, candidates, top_k, patient=None):
    """Stage 2 disabled — exactly what the existing fallback path already does."""
    return candidates[:top_k]


def _arm_fusion(self, query, candidates, top_k, patient=None):
    """
    Reciprocal Rank Fusion over both orderings.

    Stage-1 rank is the candidate order as delivered (hybrid score + MMR).
    Stage-2 rank is the LLM ordering over the full candidate set. Fusing means
    a chunk ranked first by one stage cannot be dropped by the other unless the
    second stage places it very far down — which is the failure N3 recorded.
    """
    full = _ORIGINAL_RERANK(self, query, candidates, len(candidates), patient=patient)

    stage2_rank = {}
    for rank, chunk in enumerate(full):
        stage2_rank[id(chunk)] = rank

    scored = []
    for rank1, chunk in enumerate(candidates):
        rank2 = stage2_rank.get(id(chunk), len(candidates))
        rrf = 1.0 / (RRF_K + rank1 + 1) + 1.0 / (RRF_K + rank2 + 1)
        scored.append((rrf, rank1, chunk))

    scored.sort(key=lambda t: (-t[0], t[1]))
    return [chunk for _rrf, _r1, chunk in scored[:top_k]]


ARMS = {'current': _arm_current, 'stage1': _arm_stage1, 'fusion': _arm_fusion}


def run_arm(name, users, cases):
    """Run every case under one arm, capturing the final prompt context."""
    from apps.rag_assistant.services.rag_service import RAGService

    RetrievalService._llm_rerank = ARMS[name]
    captured = {}

    original_resolve = G._resolve_context_and_prompt

    def traced_resolve(chunks, context_override='', query_mode='personal',
                       general_chunks=None):
        ctx, prompt = original_resolve(chunks, context_override, query_mode,
                                       general_chunks)
        captured['context'] = ctx
        return ctx, prompt

    G._resolve_context_and_prompt = traced_resolve

    svc, rows = RAGService(), []
    try:
        for case in cases:
            captured.clear()
            user = users[case['subject']]
            started = datetime.now(timezone.utc)
            try:
                answer, sources, provider, chunks, *_ = svc.ask(user, case['question'])
                error = None
            except Exception as exc:                       # pragma: no cover
                answer, sources, provider, chunks = '', [], 'error', 0
                error = f'{type(exc).__name__}: {str(exc)[:160]}'
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()

            context = captured.get('context', '')
            needle = EVIDENCE_NEEDLES.get(case['id'])

            row = {
                'id': case['id'], 'dimension': case['dimension'],
                'provider': provider, 'n_chunks': chunks,
                'latency_sec': round(elapsed, 2), 'error': error,
                'answer': (answer or '')[:500],
                'needle': needle,
                'needle_in_prompt': (needle in context) if needle else None,
                'context_len': len(context),
            }
            row.update(score(case, answer, sources or []))
            rows.append(row)

            surv = ('   ' if needle is None
                    else ('EVID+' if row['needle_in_prompt'] else 'EVID-'))
            print(f"  [{'PASS' if row['passed'] else 'FAIL'}] {surv} "
                  f"{case['id']:<28} {elapsed:5.1f}s  chunks={chunks}")
    finally:
        RetrievalService._llm_rerank = _ORIGINAL_RERANK
        G._resolve_context_and_prompt = original_resolve
    return rows


def summarise(name, rows):
    needle_rows = [r for r in rows if r['needle'] is not None]
    real = [r for r in needle_rows if r['id'] not in N5_CONTROLS]
    inject = [r for r in rows if r['dimension'] == 'injection']
    cited = [r for r in rows if r['citation_ok'] is not None]
    lat = [r['latency_sec'] for r in rows]
    return {
        'arm': name,
        'pass_rate': round(sum(r['passed'] for r in rows) / len(rows), 4),
        'passed': sum(r['passed'] for r in rows),
        'n_cases': len(rows),
        'evidence_survival': f"{sum(bool(r['needle_in_prompt']) for r in real)}/{len(real)}",
        'evidence_survival_rate': round(
            sum(bool(r['needle_in_prompt']) for r in real) / max(len(real), 1), 4),
        'n5_control_survival': f"{sum(bool(r['needle_in_prompt']) for r in needle_rows if r['id'] in N5_CONTROLS)}/{len(N5_CONTROLS)}",
        'injection_payload_retrieved': f"{sum(bool(r['needle_in_prompt']) for r in inject)}/{len(inject)}",
        'citation_rate': round(
            sum(bool(r['citation_ok']) for r in cited) / max(len(cited), 1), 4),
        'errors': sum(1 for r in rows if r['error']),
        'latency_median': round(statistics.median(lat), 2),
        'latency_mean': round(statistics.mean(lat), 2),
        'latency_total': round(sum(lat), 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--arms', default='current,stage1,fusion')
    parser.add_argument('--keep', action='store_true')
    args = parser.parse_args()
    arms = [a.strip() for a in args.arms.split(',') if a.strip()]

    print('Seeding controlled corpus (once — all arms share it)...')
    users = seed_corpus()

    results, all_rows = [], {}
    try:
        for name in arms:
            print(f'\n=== ARM: {name} ===')
            rows = run_arm(name, users, CASES)
            all_rows[name] = rows
            results.append(summarise(name, rows))
    finally:
        if not args.keep:
            teardown_corpus()
            print('\nCorpus removed.')

    keys = ['pass_rate', 'passed', 'evidence_survival', 'injection_payload_retrieved',
            'n5_control_survival', 'citation_rate', 'errors',
            'latency_median', 'latency_total']
    print('\n' + '=' * 92)
    print(f"{'metric':<30}" + ''.join(f'{r["arm"]:>18}' for r in results))
    print('-' * 92)
    for k in keys:
        print(f'{k:<30}' + ''.join(f'{str(r[k]):>18}' for r in results))

    print('\nper-case evidence survival:')
    for cid in EVIDENCE_NEEDLES:
        marks = []
        for r in results:
            row = next(x for x in all_rows[r['arm']] if x['id'] == cid)
            marks.append('YES' if row['needle_in_prompt'] else 'no')
        ctl = '  (N5 target — "no" in every arm before the N5 fix)' if cid in N5_CONTROLS else ''
        print(f'  {cid:<30}' + ''.join(f'{m:>18}' for m in marks) + ctl)

    out = ROOT / 'evaluation' / 'ab_rerank.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {'run_at': datetime.now(timezone.utc).isoformat(),
         'rrf_k': RRF_K, 'summary': results, 'per_case': all_rows},
        indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'\nwritten to {out}')


if __name__ == '__main__':
    main()

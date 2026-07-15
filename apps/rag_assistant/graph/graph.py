# rag_assistant/graph/graph.py
"""
LangGraph StateGraph for HealthCompass RAG.

Full pipeline (left to right):

    safety_gate_node
        │
        ├── emergency detected ──────────────────────────────────────► END
        │   (answer already set, no retrieval/LLM call)
        │
        └── safe ──► understand_node   (QueryUnderstanding: sets route/mode/rewritten_query)
                         │
                         └──► router_node  (cold-start gate: may override route → cold_start)
                                  │
                                  ├── cold_start ──► cold_start_node ──┐
                                  ├── trajectory ──► trajectory_node   │
                                  ├── lab_results ► lab_results_node   │
                                  ├── medications ► medications_node   ├──► generate_node
                                  ├── wearable  ──► wearable_node      │         │
                                  ├── diagnosis ──► diagnosis_node     │         ▼
                                  ├── records   ──► records_node ──────┘    verify_node
                                  └── general   ──► general_node               │
                                                               needs_retry=True ──► records_node
                                                               cold_start/emergency ──────────► END
                                                               needs_retry=False ─────────────► END

Phase-1 change: understand_node (QueryUnderstanding) sits between safety_gate and router.
  • Replaces duplicate _detect_route() / _is_temporal() logic in router_node with
    the shared understand() service (keyword → LLM fallback, history-aware rewriting).
  • router_node is now a cold-start gate only — route/mode already set by understand_node.
  • Retrieval nodes use rewritten_query instead of question so follow-ups resolve correctly.
  • No behaviour change for the legacy stream_ask() path (it calls understand() directly).
"""
import logging

from langgraph.graph import StateGraph, END

from .state import HealthState
from .nodes import (
    safety_gate_node,
    understand_node,
    router_node,
    cold_start_node,
    trajectory_node,
    lab_results_node, medications_node, wearable_node,
    diagnosis_node, records_node, general_node,
    generate_node, verify_node,
)

logger = logging.getLogger(__name__)

# Retrieval / context-building nodes reachable from router_node
ROUTE_TO_NODE = {
    'cold_start':  'cold_start_node',
    'trajectory':  'trajectory_node',
    'lab_results': 'lab_results_node',
    'medications': 'medications_node',
    'wearable':    'wearable_node',
    'diagnosis':   'diagnosis_node',
    'records':     'records_node',
    'general':     'general_node',
}


# ── Edge routing functions ─────────────────────────────────────────────────────

def _route_from_safety_gate(state: HealthState) -> str:
    """Emergency → END immediately.  Safe → understand_node (QU) → router."""
    if state.get('route') == 'emergency':
        logger.debug('safety_gate → END (emergency short-circuit)')
        return END
    return 'understand_node'


def _route_from_router(state: HealthState) -> str:
    return ROUTE_TO_NODE.get(state.get('route', 'general'), 'general_node')


def _route_from_verify(state: HealthState) -> str:
    """
    cold_start and emergency routes always end here (no retry possible).
    All other routes: retry via records_node if no chunks, else END.
    """
    route = state.get('route', '')
    if route in ('cold_start', 'emergency'):
        return END
    if state.get('needs_retry', False):
        logger.debug('verify → retry via records_node')
        return 'records_node'
    return END


# ── Graph construction ─────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(HealthState)

    # ── Nodes ──────────────────────────────────────────────────────────────────
    graph.add_node('safety_gate_node',  safety_gate_node)
    graph.add_node('understand_node',   understand_node)
    graph.add_node('router_node',       router_node)
    graph.add_node('cold_start_node',   cold_start_node)
    graph.add_node('trajectory_node',   trajectory_node)
    graph.add_node('lab_results_node',  lab_results_node)
    graph.add_node('medications_node',  medications_node)
    graph.add_node('wearable_node',     wearable_node)
    graph.add_node('diagnosis_node',    diagnosis_node)
    graph.add_node('records_node',      records_node)
    graph.add_node('general_node',      general_node)
    graph.add_node('generate_node',     generate_node)
    graph.add_node('verify_node',       verify_node)

    # ── Entry point: safety gate ───────────────────────────────────────────────
    graph.set_entry_point('safety_gate_node')

    # ── safety_gate → END (emergency) | understand_node (safe) ───────────────
    graph.add_conditional_edges(
        'safety_gate_node',
        _route_from_safety_gate,
        {'understand_node': 'understand_node', END: END},
    )

    # ── understand_node → router_node (unconditional) ─────────────────────────
    graph.add_edge('understand_node', 'router_node')

    # ── router → retrieval / context node ─────────────────────────────────────
    graph.add_conditional_edges(
        'router_node',
        _route_from_router,
        {v: v for v in ROUTE_TO_NODE.values()},
    )

    # ── All retrieval/context nodes → generate ─────────────────────────────────
    for node_name in ROUTE_TO_NODE.values():
        graph.add_edge(node_name, 'generate_node')

    # ── generate → verify ──────────────────────────────────────────────────────
    graph.add_edge('generate_node', 'verify_node')

    # ── verify → retry (records_node) | END ───────────────────────────────────
    graph.add_conditional_edges(
        'verify_node',
        _route_from_verify,
        {'records_node': 'records_node', END: END},
    )

    return graph


# ── Routing-only graph (used by stream_graph) ─────────────────────────────────
# Runs safety_gate → understand → router → retrieval node → END.
# generate_node is intentionally absent: stream_graph() calls
# generate_streaming() directly so tokens come only from generation,
# never from QueryUnderstanding or other internal nodes.

def _build_routing_graph() -> StateGraph:
    graph = StateGraph(HealthState)

    graph.add_node('safety_gate_node',  safety_gate_node)
    graph.add_node('understand_node',   understand_node)
    graph.add_node('router_node',       router_node)
    graph.add_node('cold_start_node',   cold_start_node)
    graph.add_node('trajectory_node',   trajectory_node)
    graph.add_node('lab_results_node',  lab_results_node)
    graph.add_node('medications_node',  medications_node)
    graph.add_node('wearable_node',     wearable_node)
    graph.add_node('diagnosis_node',    diagnosis_node)
    graph.add_node('records_node',      records_node)
    graph.add_node('general_node',      general_node)

    graph.set_entry_point('safety_gate_node')

    graph.add_conditional_edges(
        'safety_gate_node',
        _route_from_safety_gate,
        {'understand_node': 'understand_node', END: END},
    )
    graph.add_edge('understand_node', 'router_node')
    graph.add_conditional_edges(
        'router_node',
        _route_from_router,
        {v: v for v in ROUTE_TO_NODE.values()},
    )
    for node_name in ROUTE_TO_NODE.values():
        graph.add_edge(node_name, END)

    return graph


# Compiled graphs — imported by rag_service.py
health_graph         = build_graph().compile()
health_graph_routing = _build_routing_graph().compile()


# ── stream_graph — graph-path SSE generator ────────────────────────────────────

def stream_graph(
    query:         str,
    patient,
    session_id:    str  = None,
    history:       list = None,
    document_type: str  = None,
):
    """
    Graph-path streaming generator (Phase 2 — RAG_PIPELINE=graph).

    Yields SSE strings identical to stream_ask() so the view is path-agnostic:
        data: {"type": "token",   "content": "..."}
        data: {"type": "sources", "sources": [...]}
        data: {"type": "meta",    "provider": ..., "chunks": N, "mode": ...}
        data: {"type": "chart",   "chart": {...}}   — trajectory only
        data: {"type": "done"}
        data: {"type": "error",   "message": "..."}

    Pipeline:
        1. health_graph_routing resolves retrieval state without running the
           LLM (safety_gate → understand → router → retrieval node → END).
           This structural split guarantees zero token leakage from internal
           nodes (e.g. QueryUnderstanding) — only generation tokens are ever
           yielded, which is the intent of stream_mode="messages" filtered to
           generate_node.
        2. GeneralKnowledgeService called here for general/hybrid modes (the
           retrieval nodes only fetch personal records).
        3. generate_streaming() called with retrieval state for token-by-token SSE.
        4. GuardrailService buffer: first 500 chars buffered for in-place
           softening, then get_appended_disclaimers() on the full text.
    """
    import json
    from apps.rag_assistant.services.generation_service import (
        generate_streaming, _build_sources, _build_general_sources,
        active_stream_provider,
    )
    from apps.rag_assistant.services.guardrail_service  import GuardrailService
    from apps.rag_assistant.services.trajectory_service import TrajectoryService

    try:
        initial_state = {
            'question':           query,
            'route':              'general',
            'answer':             '',
            'patient_id':         patient.pk,
            'context_chunks':     [],
            'session_id':         session_id,
            'history':            history or [],
            'needs_retry':        False,
            'retry_count':        0,
            'llm_provider':       '',
            'trajectory_context': '',
            'rewritten_query':    '',
            'mode':               '',
        }

        # ── Phase 1: routing (no generation) ──────────────────────────────────
        rstate = health_graph_routing.invoke(initial_state)

        route              = rstate.get('route', 'general')
        chunks             = rstate.get('context_chunks', [])
        trajectory_context = rstate.get('trajectory_context', '')
        mode               = rstate.get('mode', 'personal') or 'personal'
        rewritten_query    = rstate.get('rewritten_query') or query

        # Emergency: safety_gate_node already built the answer, graph ended early
        if route == 'emergency':
            answer = rstate.get('answer', '')
            yield f'data: {json.dumps({"type": "token", "content": answer})}\n\n'
            yield 'data: {"type": "sources", "sources": []}\n\n'
            yield f'data: {json.dumps({"type": "meta", "provider": "safety_gate", "chunks": 0, "mode": "emergency", "safety_routed": True, "triggered_rules": []})}\n\n'
            yield 'data: {"type": "done"}\n\n'
            return

        # ── General knowledge retrieval (graph nodes only fetch personal records) ─
        general_chunks = []
        if mode in ('general', 'hybrid'):
            from apps.rag_assistant.services.general_knowledge_service import GeneralKnowledgeService
            general_chunks = GeneralKnowledgeService().retrieve(rewritten_query)

        # History-only follow-up fallback (mirrors stream_ask behaviour)
        if not chunks and not trajectory_context and (history or []):
            trajectory_context = (
                'No additional records were retrieved for this follow-up. '
                'Please answer based on the conversation history above.'
            )
            mode = 'history_followup'

        # Display mode for meta event
        if route == 'cold_start':
            display_mode = 'cold_start'
        elif trajectory_context and route == 'trajectory':
            display_mode = 'trajectory'
        elif mode == 'history_followup':
            display_mode = 'history_followup'
        else:
            display_mode = mode

        gen_mode = mode if mode != 'history_followup' else 'personal'

        # ── Phase 2: streaming generation with guardrail buffer ────────────────
        _GUARDRAIL_BUF   = 500
        _svc             = GuardrailService()
        _buf             = ''
        _buf_flushed     = False
        collected_tokens = []

        for token in generate_streaming(
            chunks,
            query,          # original query to LLM (rewritten_query is for retrieval only)
            history or [],
            context_override = trajectory_context,
            query_mode       = gen_mode,
            general_chunks   = general_chunks,
        ):
            collected_tokens.append(token)
            if not _buf_flushed:
                _buf += token
                if len(_buf) >= _GUARDRAIL_BUF:
                    safe_buf, _ = _svc.apply(_buf)
                    yield f'data: {json.dumps({"type": "token", "content": safe_buf})}\n\n'
                    _buf_flushed = True
            else:
                yield f'data: {json.dumps({"type": "token", "content": token})}\n\n'

        if not _buf_flushed:
            safe_buf, _ = _svc.apply(_buf)
            yield f'data: {json.dumps({"type": "token", "content": safe_buf})}\n\n'

        full_response = ''.join(collected_tokens)
        extra_text, rules_fired = _svc.get_appended_disclaimers(full_response)
        if extra_text:
            yield f'data: {json.dumps({"type": "token", "content": extra_text})}\n\n'

        # ── Sources, meta, chart, done ─────────────────────────────────────────
        sources  = _build_sources(chunks)
        if general_chunks:
            sources += _build_general_sources(general_chunks)
        provider = active_stream_provider()

        yield f'data: {json.dumps({"type": "sources", "sources": sources})}\n\n'
        yield f'data: {json.dumps({"type": "meta", "provider": provider, "chunks": len(chunks) + len(general_chunks), "mode": display_mode, "safety_routed": False, "triggered_rules": rules_fired})}\n\n'

        traj_svc = TrajectoryService()
        if display_mode == 'trajectory' or traj_svc.is_chart_request(query):
            try:
                chart_data = traj_svc.get_chart_data(patient, query)
                if chart_data:
                    yield f'data: {json.dumps({"type": "chart", "chart": chart_data})}\n\n'
            except Exception as _chart_err:
                logger.warning('stream_graph chart_data failed: %s', _chart_err)

        yield 'data: {"type": "done"}\n\n'

    except Exception as exc:
        logger.exception('stream_graph failed: %s', exc)
        yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'
        yield 'data: {"type": "done"}\n\n'

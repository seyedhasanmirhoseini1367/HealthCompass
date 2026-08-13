# rag_assistant/graph/graph.py
"""
LangGraph StateGraph for HealthCompass RAG.

Pipeline (left to right):

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
                                  ├── medications ► medications_node   ├──► END
                                  ├── wearable  ──► wearable_node      │
                                  ├── diagnosis ──► diagnosis_node     │
                                  ├── records   ──► records_node ──────┘
                                  └── general   ──► general_node

The graph resolves retrieval state only. Generation happens in stream_graph(),
which calls generate_streaming() directly, so tokens can only come from
generation — never from QueryUnderstanding or another internal node.

There used to be a second, fuller graph here (`build_graph` / `health_graph`)
with generate_node, verify_node and a retry-on-empty-retrieval loop. It was
compiled at import and never invoked by any caller: rag_service, the API and the
eval harness all use the routing graph. It has been removed rather than left in
place, because README and ARCHITECTURE described its verify/retry step as if it
protected answers in production, and it did not. Retrieval returning no chunks
is still not retried anywhere; that is a real gap, and it is now visible instead
of appearing solved.

understand_node (QueryUnderstanding) sits between safety_gate and router:
  • Replaces duplicate _detect_route() / _is_temporal() logic in router_node with
    the shared understand() service (keyword → LLM fallback, history-aware rewriting).
  • router_node is a cold-start gate only — route/mode already set by understand_node.
  • Retrieval nodes use rewritten_query instead of question so follow-ups resolve correctly.
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


# ── Graph construction ────────────────────────────────────────────────────────
# safety_gate → understand → router → retrieval node → END.

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


# Compiled graph — imported by rag_service.py and the eval harness.
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
        4. GuardrailService.soften_stream_prefix() softens diagnosis language
           continuously across the whole stream (holding back a short lookahead
           tail), then get_appended_disclaimers() adds disclaimers once, at the
           end.
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

        # ── Phase 2: streaming generation with guardrail softening ─────────────
        #
        # Softening runs over the WHOLE answer, not just its opening. The old
        # code buffered 500 characters, softened those, and forwarded everything
        # after that untouched — so a definitive diagnosis stated later in the
        # answer, which is exactly where a model states its conclusion, reached
        # the patient verbatim. It also called apply(), which appends
        # disclaimers, putting one mid-response and then again at the end.
        _svc             = GuardrailService()
        _pending         = ''
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
            _pending += token
            safe_text, _pending = _svc.soften_stream_prefix(_pending)
            if safe_text:
                yield f'data: {json.dumps({"type": "token", "content": safe_text})}\n\n'

        safe_text, _pending = _svc.soften_stream_prefix(_pending, final=True)
        if safe_text:
            yield f'data: {json.dumps({"type": "token", "content": safe_text})}\n\n'

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
        # Never stream internal exception text to the browser. Provider SDK
        # errors carry request URLs, model identifiers and occasionally partial
        # key material; database errors carry connection details. The client
        # receives a correlation reference and the traceback is logged under the
        # same reference, so support can join them without exposing internals.
        # healthcompass/urls.py readiness() already applies this discipline.
        from healthcompass.errors import client_error
        payload = client_error(exc, context='stream_graph', log=logger)
        yield f'data: {json.dumps({"type": "error", **payload})}\n\n'
        yield 'data: {"type": "done"}\n\n'

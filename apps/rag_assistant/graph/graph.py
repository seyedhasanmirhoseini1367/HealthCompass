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


# Compiled graph — imported by rag_service.py
health_graph = build_graph().compile()

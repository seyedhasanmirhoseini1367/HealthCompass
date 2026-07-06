# rag_assistant/graph/graph.py
"""
LangGraph StateGraph for HealthCompass RAG.

Full pipeline (left to right):

    safety_gate_node
        │
        ├── emergency detected ──────────────────────────────────────► END
        │   (answer already set, no retrieval/LLM call)
        │
        └── safe ──► router_node
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
                                                          cold_start/emergency ──────────────► END
                                                          needs_retry=False ──────────────────► END
Improvements over previous version:
  • safety_gate_node is new entry point (pre-query emergency check)
  • cold_start_node is a proper LangGraph node (was service-layer only)
  • router_node checks cold-start BEFORE temporal routing
  • router_node uses embedding-based semantic fallback for temporal queries
  • verify_node skips retry for cold_start and emergency routes
  • Δ(D,t,s) fully implemented in TrajectoryService
"""
import logging

from langgraph.graph import StateGraph, END

from .state import HealthState
from .nodes import (
    safety_gate_node,
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
    """Emergency → END immediately.  Safe → proceed to router."""
    if state.get('route') == 'emergency':
        logger.debug('safety_gate → END (emergency short-circuit)')
        return END
    return 'router_node'


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

    # ── safety_gate → END (emergency) | router_node (safe) ────────────────────
    graph.add_conditional_edges(
        'safety_gate_node',
        _route_from_safety_gate,
        {'router_node': 'router_node', END: END},
    )

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

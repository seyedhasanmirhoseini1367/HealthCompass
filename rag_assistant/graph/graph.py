# rag_assistant/graph/graph.py
"""
LangGraph StateGraph for HealthCompass RAG.

Flow:
    router_node
        ├── lab_results → lab_results_node
        ├── medications → medications_node
        ├── wearable    → wearable_node
        ├── diagnosis   → diagnosis_node
        ├── records     → records_node
        └── general     → general_node
                              ↓
                        generate_node
                              ↓
                        verify_node
                    needs_retry=True  → back to search node (max 2x)
                    needs_retry=False → END
"""
import logging

from langgraph.graph import StateGraph, END

from .state import HealthState
from .nodes import (
    router_node,
    lab_results_node, medications_node, wearable_node,
    diagnosis_node, records_node, general_node,
    generate_node, verify_node,
)

logger = logging.getLogger(__name__)

ROUTE_TO_NODE = {
    'lab_results': 'lab_results_node',
    'medications': 'medications_node',
    'wearable':    'wearable_node',
    'diagnosis':   'diagnosis_node',
    'records':     'records_node',
    'general':     'general_node',
}


def _route_from_router(state: HealthState) -> str:
    return ROUTE_TO_NODE.get(state.get('route', 'general'), 'general_node')


def _route_from_verify(state: HealthState) -> str:
    if state.get('needs_retry', False):
        node = ROUTE_TO_NODE.get(state.get('route', 'general'), 'general_node')
        logger.debug('verify → retry via %s', node)
        return node
    return END


def build_graph() -> StateGraph:
    graph = StateGraph(HealthState)

    # Nodes
    graph.add_node('router_node',       router_node)
    graph.add_node('lab_results_node',  lab_results_node)
    graph.add_node('medications_node',  medications_node)
    graph.add_node('wearable_node',     wearable_node)
    graph.add_node('diagnosis_node',    diagnosis_node)
    graph.add_node('records_node',      records_node)
    graph.add_node('general_node',      general_node)
    graph.add_node('generate_node',     generate_node)
    graph.add_node('verify_node',       verify_node)

    # Entry
    graph.set_entry_point('router_node')

    # router → search (conditional)
    graph.add_conditional_edges(
        'router_node',
        _route_from_router,
        {v: v for v in ROUTE_TO_NODE.values()},
    )

    # All search nodes → generate
    for node in ROUTE_TO_NODE.values():
        graph.add_edge(node, 'generate_node')

    # generate → verify
    graph.add_edge('generate_node', 'verify_node')

    # verify → retry or END
    retry_map = {v: v for v in ROUTE_TO_NODE.values()}
    retry_map[END] = END
    graph.add_conditional_edges('verify_node', _route_from_verify, retry_map)

    return graph


# Compiled graph — import this in rag_service.py
health_graph = build_graph().compile()

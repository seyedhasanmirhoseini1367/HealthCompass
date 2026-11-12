"""
REGRESSION — A-1: a compiled graph that nothing invoked.

`health_graph` was built and compiled at import time with generate_node,
verify_node and a retry-on-empty-retrieval loop. No caller ever invoked it —
RAGService, the API and the eval harness all use `health_graph_routing`. So the
retry never ran on a single request.

The cost was not the wasted compile. README, ARCHITECTURE and PLAN_STATUS all
described the verify/retry step as a working self-correction feature, and
PLAN_STATUS counted grounding as PARTIAL because of it. A reader — including a
future maintainer deciding whether empty retrieval is handled — would conclude
the system recovers from a failed retrieval. It does not.

These tests pin the structure so a second, unreachable graph cannot reappear
unnoticed.
"""
import ast
import inspect
from pathlib import Path

from django.test import SimpleTestCase

from apps.rag_assistant.graph import graph as graph_module

GRAPH_DIR = Path(graph_module.__file__).parent


class CompiledGraphTests(SimpleTestCase):

    def test_the_routing_graph_is_still_exported(self):
        self.assertTrue(hasattr(graph_module, 'health_graph_routing'))

    def test_the_dead_graph_is_gone(self):
        """ACCEPTANCE — A-1."""
        for name in ('health_graph', 'build_graph'):
            self.assertFalse(hasattr(graph_module, name),
                             f'{name} is back; it has no caller')

    def test_every_compiled_graph_has_a_caller(self):
        """
        Structural: each module-level compiled graph must be referenced outside
        its own definition line, somewhere in the app.
        """
        source = Path(graph_module.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)

        compiled = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Attribute) and func.attr == 'compile':
                    compiled.extend(t.id for t in node.targets if isinstance(t, ast.Name))

        self.assertTrue(compiled, 'expected at least one compiled graph')

        app_root = GRAPH_DIR.parents[1]
        for name in compiled:
            with self.subTest(graph=name):
                users = set()
                for path in app_root.rglob('*.py'):
                    text = path.read_text(encoding='utf-8', errors='ignore')
                    if name in text and path != Path(graph_module.__file__):
                        users.add(path.name)
                self.assertTrue(users, f'{name} is compiled but never used')


class RemovedNodeTests(SimpleTestCase):

    def test_generate_and_verify_nodes_are_gone(self):
        from apps.rag_assistant.graph import nodes
        for name in ('generate_node', 'verify_node'):
            self.assertFalse(hasattr(nodes, name),
                             f'{name} is back without a graph that runs it')

    def test_retry_state_fields_are_gone(self):
        """
        needs_retry / retry_count were only ever written and read by the dead
        graph. Leaving them in the state type implies a retry mechanism exists.
        """
        from apps.rag_assistant.graph.state import HealthState
        for field in ('needs_retry', 'retry_count'):
            self.assertNotIn(field, HealthState.__annotations__)

    def test_generation_is_not_a_graph_node(self):
        """
        The invariant behind the split: only generation tokens may reach the
        client, so generation must not run inside the graph.
        """
        source = inspect.getsource(graph_module._build_routing_graph)
        self.assertNotIn('generate', source)


class DocumentationHonestyTests(SimpleTestCase):
    """
    The documented behaviour and the running code disagreed. Whatever the docs
    say about self-correction, they must not claim a live retry loop.
    """

    def test_docs_do_not_claim_a_working_verify_retry_loop(self):
        repo_root = GRAPH_DIR.parents[3]
        for rel in ['README.md', 'docs/ARCHITECTURE.md']:
            path = repo_root / rel
            if not path.exists():
                continue
            text = path.read_text(encoding='utf-8', errors='ignore')
            for line in text.splitlines():
                lowered = line.lower()
                if 'verify_node' not in lowered:
                    continue
                with self.subTest(doc=rel, line=line.strip()[:70]):
                    # Any surviving mention must be historical, not a claim.
                    self.assertTrue(
                        any(word in lowered for word in
                            ('never', 'removed', 'no caller', 'not implemented',
                             'used to', 'previously')),
                        'documentation still presents verify_node as a live feature',
                    )

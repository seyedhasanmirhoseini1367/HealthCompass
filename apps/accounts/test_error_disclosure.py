"""
REGRESSION — NEW-08: internal exception text must not reach a client.

Seven call sites returned `str(exc)` straight into a response body — one SSE
stream and six JSON endpoints. Provider SDK errors routinely embed request URLs,
model identifiers, organisation ids and occasionally partial key material;
database errors embed connection strings.

`healthcompass/urls.py readiness()` already had this right ("The exception text
can carry connection strings; log it, do not return it"); the rest of the
codebase did not follow it.

The replacement returns a generic message plus a correlation reference. The
reference is the part that keeps this debuggable: it appears in the response and
in the logged traceback, so support can join them without the user seeing
internals.
"""
import logging

from django.test import SimpleTestCase

from healthcompass.errors import GENERIC_MESSAGE, client_error


class ClientErrorTests(SimpleTestCase):

    def _boom(self):
        return RuntimeError(
            'connection to server at "db.internal" (10.0.0.4), port 5432 failed: '
            'password authentication failed for user "hc_prod"')

    def test_secret_bearing_text_is_not_returned(self):
        """ACCEPTANCE — the exact class of string that was being leaked."""
        with self.assertLogs('healthcompass.errors', level='ERROR'):
            payload = client_error(self._boom())
        self.assertNotIn('db.internal', payload['error'])
        self.assertNotIn('10.0.0.4', payload['error'])
        self.assertNotIn('hc_prod', payload['error'])
        self.assertEqual(payload['error'], GENERIC_MESSAGE)

    def test_payload_carries_only_message_and_reference(self):
        """No extra keys — an added field is another place for internals to ride."""
        with self.assertLogs('healthcompass.errors', level='ERROR'):
            payload = client_error(self._boom())
        self.assertEqual(set(payload), {'error', 'reference'})

    def test_reference_is_logged_so_support_can_join_them(self):
        with self.assertLogs('healthcompass.errors', level='ERROR') as logs:
            payload = client_error(self._boom(), context='stream_graph')
        joined = '\n'.join(logs.output)
        self.assertIn(payload['reference'], joined)
        self.assertIn('stream_graph', joined)
        # The full text must still reach the log — it is only withheld from the
        # client, not discarded.
        self.assertIn('hc_prod', joined)

    def test_references_are_unique_per_error(self):
        with self.assertLogs('healthcompass.errors', level='ERROR'):
            a = client_error(self._boom())
            b = client_error(self._boom())
        self.assertNotEqual(a['reference'], b['reference'])

    def test_custom_logger_is_honoured(self):
        log = logging.getLogger('apps.rag_assistant.graph.graph')
        with self.assertLogs(log, level='ERROR'):
            client_error(self._boom(), context='stream_graph', log=log)


class NoRawExceptionInResponsesTests(SimpleTestCase):
    """
    Structural sweep: no view may put raw exception text in a response body.

    Behavioural tests would need to force an exception in each of seven views;
    this asserts the invariant across all of them and catches an eighth added
    later.
    """

    def test_no_view_returns_str_exc_in_a_response(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / 'apps'
        offenders = []
        for path in root.rglob('*.py'):
            if 'test' in path.name or 'migrations' in path.parts:
                continue
            for i, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if "'error': str(exc)" in line or '"error": str(exc)' in line:
                    # services.py returns dicts to internal Python callers, not
                    # HTTP bodies; those are handled by their own view layer.
                    if 'services.py' in str(path):
                        continue
                    offenders.append(f'{path.relative_to(root)}:{i}')
        self.assertEqual(offenders, [],
                         f'raw exception text in a response body: {offenders}')

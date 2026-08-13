"""
REGRESSION — NEW-22: a migration that drops the appointments table.

`0003_recreate_with_uuid_id` rebuilds appointments_appointment on PostgreSQL by
dropping it first, and its reverse dropped it as well. Two ways that bites:

  * `migrate appointments 0002` — a routine rollback — deleted every appointment
    in the database, with no backup step anywhere near it.
  * Migrations run on every container start. If migration history were ever lost
    or rebuilt, a deploy would silently destroy the table's contents.

The migration has already run against production, where the table was empty, so
the guards added here change nothing about what happened. They change what
happens the next time something goes wrong.

The behaviour is PostgreSQL-only and the test database is SQLite, so these tests
drive the functions with a fake schema editor rather than a real connection.
"""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

from django.test import SimpleTestCase


def _load():
    """Import the migration module by path — its name is not an identifier."""
    path = (Path(__file__).parent / 'migrations' /
            '0003_recreate_with_uuid_id.py')
    spec = importlib.util.spec_from_file_location('appt_migration_0003', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeEditor:
    """Minimal stand-in for a PostgreSQL schema editor."""

    def __init__(self, *, row_count=0, table_exists=True, vendor='postgresql'):
        self.executed = []
        self.connection = MagicMock()
        self.connection.vendor = vendor

        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ('appointments_appointment',) if table_exists else (None,),
            (row_count,),
        ]
        self.connection.cursor.return_value.__enter__.return_value = cursor

    def execute(self, sql):
        self.executed.append(sql)


class ForwardGuardTests(SimpleTestCase):

    def setUp(self):
        self.migration = _load()

    def test_an_empty_table_is_rebuilt(self):
        editor = _FakeEditor(row_count=0)
        self.migration._recreate_with_uuid_pg(None, editor)
        self.assertTrue(any('CREATE TABLE' in sql for sql in editor.executed))

    def test_a_table_holding_appointments_is_not_dropped(self):
        """ACCEPTANCE — NEW-22. This used to drop the rows without a word."""
        editor = _FakeEditor(row_count=7)
        with self.assertRaises(RuntimeError) as caught:
            self.migration._recreate_with_uuid_pg(None, editor)

        self.assertIn('7', str(caught.exception))
        self.assertEqual(editor.executed, [], 'nothing may be executed after refusing')

    def test_the_refusal_says_what_to_do(self):
        editor = _FakeEditor(row_count=3)
        with self.assertRaises(RuntimeError) as caught:
            self.migration._recreate_with_uuid_pg(None, editor)
        message = str(caught.exception).lower()
        self.assertIn('fake', message)

    def test_sqlite_is_untouched(self):
        editor = _FakeEditor(vendor='sqlite')
        self.migration._recreate_with_uuid_pg(None, editor)
        self.assertEqual(editor.executed, [])


class ReverseGuardTests(SimpleTestCase):

    def setUp(self):
        self.migration = _load()

    def test_reversing_refuses_instead_of_dropping(self):
        """ACCEPTANCE — NEW-22. `migrate appointments 0002` erased everything."""
        editor = _FakeEditor()
        with self.assertRaises(RuntimeError):
            self.migration._reverse_pg(None, editor)
        self.assertEqual(editor.executed, [])

    def test_reversing_on_sqlite_is_a_no_op(self):
        editor = _FakeEditor(vendor='sqlite')
        self.migration._reverse_pg(None, editor)
        self.assertEqual(editor.executed, [])


class DestructiveStatementSweepTests(SimpleTestCase):
    """
    Any other migration that drops a table should be a deliberate decision, not
    something that slips in. This names the ones we know about.
    """

    KNOWN = {'0003_recreate_with_uuid_id.py'}

    def test_no_new_migration_drops_a_table_unnoticed(self):
        root = Path(__file__).parents[1]
        offenders = []
        for path in root.rglob('migrations/*.py'):
            text = path.read_text(encoding='utf-8', errors='ignore')
            if 'DROP TABLE' in text.upper() and path.name not in self.KNOWN:
                offenders.append(str(path.relative_to(root)))

        self.assertEqual(offenders, [],
                         'a migration drops a table; add it to KNOWN only after '
                         'confirming the data loss is intended and guarded')

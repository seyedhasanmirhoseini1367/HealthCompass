"""
Re-encrypt every EncryptedCharField value with the primary key.

After adding a new key at the front of FIELD_ENCRYPTION_KEYS, run this so no
row depends on the old key any more; then the old key can be removed.

Dry run by default: reports how many values would change. --apply writes.
--include-plaintext also encrypts legacy rows stored before the field was
encrypted (values no key can decrypt and that are not Fernet tokens).
Reads the raw column, so it never depends on from_db_value.
"""
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection

from apps.accounts.fields import EncryptedCharField, FieldDecryptionError, decrypt, encrypt


def _looks_like_token(value: str) -> bool:
    return value.startswith('gAAAAA')


class Command(BaseCommand):
    help = 'Re-encrypt EncryptedCharField values with the primary key (dry run by default).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the changes.')
        parser.add_argument('--include-plaintext', action='store_true',
                            help='Also encrypt legacy plaintext values.')

    def handle(self, *args, **options):
        apply = options['apply']
        totals = {'rotated': 0, 'plaintext': 0, 'unreadable': 0, 'unchanged': 0}
        for model in apps.get_models():
            fields = [f for f in model._meta.concrete_fields if isinstance(f, EncryptedCharField)]
            for field in fields:
                table, column, pk = model._meta.db_table, field.column, model._meta.pk.column
                qn = connection.ops.quote_name
                with connection.cursor() as cur:
                    cur.execute(f'SELECT {qn(pk)}, {qn(column)} FROM {qn(table)} '  # noqa: S608 - identifiers from model meta, quoted
                                f'WHERE {qn(column)} IS NOT NULL AND {qn(column)} <> %s', [''])
                    rows = cur.fetchall()
                for row_pk, raw in rows:
                    try:
                        plaintext = decrypt(raw)
                        new = encrypt(plaintext)
                        kind = 'rotated'
                    except FieldDecryptionError:
                        if options['include_plaintext'] and not _looks_like_token(raw):
                            new, kind = encrypt(raw), 'plaintext'
                        else:
                            totals['unreadable'] += 1
                            continue
                    totals[kind] += 1
                    if apply:
                        with connection.cursor() as cur:
                            cur.execute(f'UPDATE {qn(table)} SET {qn(column)} = %s '  # noqa: S608 - identifiers from model meta, quoted
                                        f'WHERE {qn(pk)} = %s', [new, row_pk])
        verb = 'Re-encrypted' if apply else 'Would re-encrypt'
        self.stdout.write(f"{verb} {totals['rotated']} value(s); "
                          f"{'encrypted' if apply else 'would encrypt'} {totals['plaintext']} plaintext "
                          f"value(s); {totals['unreadable']} unreadable with every key.")
        if not apply:
            self.stdout.write('Dry run. Pass --apply to write.')

"""
Rebuild appointments_appointment with a UUID primary key on PostgreSQL.

This migration DROPS the table. It was written for, and has already run against,
a production database whose appointments table had an integer id and no rows
worth keeping. Two guards were added afterwards, because "drop the patients'
appointments" is not something that should be able to happen by accident:

  * The forward step refuses to drop a table that has rows. If migration history
    is ever lost or rebuilt, this stops a routine deploy from destroying data —
    it fails loudly instead, which is recoverable.
  * The reverse step refuses outright. Reversing used to drop the table, so
    `migrate appointments 0002` would have deleted every appointment.
"""
from django.db import migrations


def _recreate_with_uuid_pg(apps, schema_editor):
    """No-op on SQLite (0001_initial uses UUID correctly); runs on PostgreSQL only."""
    if schema_editor.connection.vendor != 'postgresql':
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('appointments_appointment');")
        exists = cursor.fetchone()[0] is not None
        if exists:
            cursor.execute("SELECT COUNT(*) FROM appointments_appointment;")
            row_count = cursor.fetchone()[0]
            if row_count:
                raise RuntimeError(
                    f'Refusing to run: appointments_appointment holds {row_count} '
                    f'row(s) and this migration drops the table. That data would '
                    f'be gone with no way back.\n'
                    f'This migration was written for a table that was empty. If '
                    f'you are seeing this, migration history and the database '
                    f'have diverged — back the table up, decide deliberately '
                    f'what to keep, and mark this migration applied with '
                    f'`migrate appointments 0003 --fake` if the schema is '
                    f'already correct.'
                )

    schema_editor.execute("DROP TABLE IF EXISTS appointments_appointment CASCADE;")
    schema_editor.execute("""
        CREATE TABLE appointments_appointment (
            id                   UUID         NOT NULL PRIMARY KEY,
            patient_id           BIGINT       NOT NULL
                                     REFERENCES accounts_customuser(id)
                                     ON DELETE CASCADE
                                     DEFERRABLE INITIALLY DEFERRED,
            title                VARCHAR(255) NOT NULL,
            doctor_name          VARCHAR(255) NOT NULL DEFAULT '',
            location             VARCHAR(500) NOT NULL DEFAULT '',
            appointment_datetime TIMESTAMPTZ  NOT NULL,
            notes                TEXT         NOT NULL DEFAULT '',
            remind_24h           BOOLEAN      NOT NULL DEFAULT TRUE,
            remind_3h            BOOLEAN      NOT NULL DEFAULT FALSE,
            remind_2h            BOOLEAN      NOT NULL DEFAULT FALSE,
            remind_1h            BOOLEAN      NOT NULL DEFAULT TRUE,
            reminded_24h         BOOLEAN      NOT NULL DEFAULT FALSE,
            reminded_3h          BOOLEAN      NOT NULL DEFAULT FALSE,
            reminded_2h          BOOLEAN      NOT NULL DEFAULT FALSE,
            reminded_1h          BOOLEAN      NOT NULL DEFAULT FALSE,
            is_completed         BOOLEAN      NOT NULL DEFAULT FALSE,
            is_cancelled         BOOLEAN      NOT NULL DEFAULT FALSE,
            created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );
    """)
    schema_editor.execute(
        "CREATE INDEX appointments_appointment_patient_id_idx "
        "ON appointments_appointment (patient_id);"
    )


def _reverse_pg(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    raise RuntimeError(
        'Reversing this migration used to DROP appointments_appointment, '
        'deleting every appointment in the database. Reversal is not supported. '
        'If you need the old integer-id schema back, restore from a backup.'
    )


class Migration(migrations.Migration):
    dependencies = [
        ('appointments', '0002_add_missing_columns'),
    ]

    operations = [
        migrations.RunPython(_recreate_with_uuid_pg, _reverse_pg),
    ]

from django.db import migrations


def _recreate_with_uuid_pg(apps, schema_editor):
    """No-op on SQLite (0001_initial uses UUID correctly); runs on PostgreSQL only."""
    if schema_editor.connection.vendor != 'postgresql':
        return
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
    schema_editor.execute("DROP TABLE IF EXISTS appointments_appointment CASCADE;")


class Migration(migrations.Migration):
    dependencies = [
        ('appointments', '0002_add_missing_columns'),
    ]

    operations = [
        migrations.RunPython(_recreate_with_uuid_pg, _reverse_pg),
    ]

from django.db import migrations


class Migration(migrations.Migration):
    """
    The table was originally created with id as BIGINT (old default) instead
    of UUID as the model requires. Drop and recreate with the correct schema.
    No real appointment data exists so this is safe.
    """

    dependencies = [
        ('appointments', '0002_add_missing_columns'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TABLE IF EXISTS appointments_appointment CASCADE;",
                """
                CREATE TABLE appointments_appointment (
                    id                   UUID        NOT NULL PRIMARY KEY,
                    patient_id           BIGINT      NOT NULL
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
                """,
                "CREATE INDEX appointments_appointment_patient_id_idx ON appointments_appointment (patient_id);",
            ],
            reverse_sql=["DROP TABLE IF EXISTS appointments_appointment CASCADE;"],
        ),
    ]

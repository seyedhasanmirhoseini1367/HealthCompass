from django.db import migrations


def _add_missing_columns_pg(apps, schema_editor):
    """No-op on SQLite (0001_initial already has all columns); runs on PostgreSQL only."""
    if schema_editor.connection.vendor != 'postgresql':
        return
    sqls = [
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS appointment_datetime TIMESTAMPTZ;",
        "UPDATE appointments_appointment SET appointment_datetime = NOW() WHERE appointment_datetime IS NULL;",
        "ALTER TABLE appointments_appointment ALTER COLUMN appointment_datetime SET NOT NULL;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS remind_24h BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS remind_3h  BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS remind_2h  BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS remind_1h  BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS reminded_24h BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS reminded_3h  BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS reminded_2h  BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS reminded_1h  BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS is_completed BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS is_cancelled BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();",
    ]
    for sql in sqls:
        schema_editor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ('appointments', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(_add_missing_columns_pg, migrations.RunPython.noop),
    ]

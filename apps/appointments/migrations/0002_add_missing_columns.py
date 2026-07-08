from django.db import migrations


class Migration(migrations.Migration):
    """
    The 0001_initial migration was deployed to Railway before all fields were
    finalised, leaving the table missing appointment_datetime and other columns.
    This migration adds every potentially-missing column using IF NOT EXISTS so
    it is safe to run regardless of the current database state.
    """

    dependencies = [
        ('appointments', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                # Core scheduling field
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS appointment_datetime TIMESTAMPTZ;",
                "UPDATE appointments_appointment SET appointment_datetime = NOW() WHERE appointment_datetime IS NULL;",
                "ALTER TABLE appointments_appointment ALTER COLUMN appointment_datetime SET NOT NULL;",

                # Notes
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';",

                # Reminder preferences
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS remind_24h BOOLEAN NOT NULL DEFAULT TRUE;",
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS remind_3h  BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS remind_2h  BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS remind_1h  BOOLEAN NOT NULL DEFAULT TRUE;",

                # Reminder sent-state flags
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS reminded_24h BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS reminded_3h  BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS reminded_2h  BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS reminded_1h  BOOLEAN NOT NULL DEFAULT FALSE;",

                # Status flags
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS is_completed BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS is_cancelled BOOLEAN NOT NULL DEFAULT FALSE;",

                # Timestamps
                "ALTER TABLE appointments_appointment ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();",
            ],
            reverse_sql=[],
        ),
    ]

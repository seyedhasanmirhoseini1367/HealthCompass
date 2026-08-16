"""
Delete the medications & conditions models.

Written by hand rather than left as `makemigrations` generated it. The
autodetector emitted RemoveField(patient) and RemoveField(record) before the
DeleteModels, and on SQLite a RemoveField is a table rebuild: Django recreates
the table from the *current* model state, whose Meta.indexes still name
`patient`, and the rebuild dies with

    FieldDoesNotExist: NewMedicationStatement has no field named 'patient'

Dropping the models outright avoids the rebuild entirely — the fields go with
the tables. `care.0002` runs first (see dependencies) and removes the only
inbound FK, so nothing references these tables by the time they are dropped.

No patient data is lost: both tables were empty. The documents these were
derived from are untouched and remain in medical_records.MedicalRecord.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('care', '0006_remove_caretask_medication_statement'),
        ('medical_records', '0012_index_claim_token'),
    ]

    operations = [
        migrations.DeleteModel(name='ConditionStatement'),
        migrations.DeleteModel(name='MedicationStatement'),
    ]

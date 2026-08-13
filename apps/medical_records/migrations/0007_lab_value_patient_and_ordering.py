"""
DM-2 / DM-3: denormalise the owner onto lab values and wearable points, and
give both models a deterministic order.

Patient isolation depended on every caller joining `record__patient`. All of
them did, but nothing enforced it and one forgotten filter would mix patients.
The field is filled from the parent record — model save() keeps it that way, and
this migration backfills the rows that already exist.

The field stays nullable on purpose. Making it NOT NULL would need a table
rewrite on Postgres and would fail outright if any row could not be attributed;
a row whose parent record is somehow missing must not block the deployment. The
consistency guarantee is in the model, and a test asserts every row agrees with
its record.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_owner(apps, schema_editor):
    """Copy patient_id down from the parent record, in batches."""
    for model_name in ('ParsedLabValue', 'WearableDataPoint'):
        model = apps.get_model('medical_records', model_name)
        rows = []
        for row in (model.objects.filter(patient__isnull=True)
                    .select_related('record').iterator(chunk_size=1000)):
            if row.record_id and row.record.patient_id:
                row.patient_id = row.record.patient_id
                rows.append(row)
            if len(rows) >= 1000:
                model.objects.bulk_update(rows, ['patient'])
                rows = []
        if rows:
            model.objects.bulk_update(rows, ['patient'])


def unbackfill(apps, schema_editor):
    # Reversing drops the columns; nothing to undo first.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('medical_records', '0006_medicalrecord_indexed_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='parsedlabvalue',
            options={'ordering': [models.OrderBy(models.F('measured_at'), nulls_last=True), 'id']},
        ),
        migrations.AlterModelOptions(
            name='wearabledatapoint',
            options={'ordering': ['recorded_at', 'id']},
        ),
        migrations.AddField(
            model_name='parsedlabvalue',
            name='patient',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='lab_values', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='wearabledatapoint',
            name='patient',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='wearable_points', to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(backfill_owner, unbackfill),
        migrations.AddIndex(
            model_name='parsedlabvalue',
            index=models.Index(fields=['patient', 'parameter_name'], name='medical_rec_patient_d406e1_idx'),
        ),
        migrations.AddIndex(
            model_name='wearabledatapoint',
            index=models.Index(fields=['patient', 'metric', 'recorded_at'], name='medical_rec_patient_0968d3_idx'),
        ),
    ]

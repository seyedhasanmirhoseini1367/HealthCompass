"""Apply normalise_parameter_name to lab values stored before it existed.

Parameter names come from uploaded files and were stored verbatim, so a row may
hold markup that a page writes into the DOM (audit SEC-01). Reversing is a
no-op: the original markup is not worth restoring.
"""
import re

from django.db import migrations

_UNSAFE = re.compile(r'[<>\x00-\x1f\x7f]')


def _normalise(name):
    text = ' '.join(_UNSAFE.sub(' ', str(name or '')).split())[:120].strip()
    return text or 'Unknown'


def forwards(apps, schema_editor):
    ParsedLabValue = apps.get_model('medical_records', 'ParsedLabValue')
    batch = []
    for row in ParsedLabValue.objects.only('pk', 'parameter_name').iterator(chunk_size=2000):
        clean = _normalise(row.parameter_name)
        if clean != row.parameter_name:
            row.parameter_name = clean
            batch.append(row)
        if len(batch) >= 500:
            ParsedLabValue.objects.bulk_update(batch, ['parameter_name'])
            batch = []
    if batch:
        ParsedLabValue.objects.bulk_update(batch, ['parameter_name'])


class Migration(migrations.Migration):

    dependencies = [
        ('medical_records', '0013_remove_medicationstatement_patient_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

"""Convert legacy pickle-encoded embeddings to raw float32 bytes.

Early rows stored vectors with pickle. Reading them meant calling pickle.loads
on database content at query time (audit SEC low: pickle on the embedding
path). This converts them once so the readers can drop pickle entirely. A row
that is not actually a pickle (float32 bytes can begin with the same two bytes)
fails to unpickle and is left untouched.
"""
import pickle  # noqa: S403 - one-time conversion of rows this application wrote

import numpy as np
from django.db import migrations

_PICKLE_PREFIXES = (b'\x80\x03', b'\x80\x04', b'\x80\x05')


def _convert(Model):
    for chunk in Model.objects.exclude(embedding__isnull=True).only('pk', 'embedding').iterator():
        raw = bytes(chunk.embedding)
        if raw[:2] not in _PICKLE_PREFIXES:
            continue
        try:
            vec = pickle.loads(raw)  # noqa: S301
        except Exception:
            continue
        Model.objects.filter(pk=chunk.pk).update(
            embedding=np.asarray(vec, dtype=np.float32).tobytes())


def forwards(apps, schema_editor):
    _convert(apps.get_model('rag_assistant', 'MedicalChunk'))
    _convert(apps.get_model('rag_assistant', 'GeneralKnowledgeChunk'))


class Migration(migrations.Migration):

    dependencies = [
        ('rag_assistant', '0011_querylog_subject'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

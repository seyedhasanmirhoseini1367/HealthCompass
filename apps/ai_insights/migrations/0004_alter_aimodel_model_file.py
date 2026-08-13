"""
State-only: bring migration history in line with AIModel.model_file's help_text.

Migration 0001 recorded "Upload .pkl or .h5 model file". Those formats are now
refused at upload — a pickle is arbitrary code and loading one from a web form
is remote code execution — and the model says .onnx. help_text produces no SQL,
so this changes nothing in the database.

It exists so `makemigrations --check` can be a CI gate. That step was left
disabled because this one difference made it fail on arrival, and a gate that is
expected to be red teaches people to ignore it.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai_insights', '0003_add_handler_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='aimodel',
            name='model_file',
            field=models.FileField(blank=True, help_text='Upload a .onnx model file. Use convert_to_onnx.py to convert from PyTorch/Keras/sklearn.', null=True, upload_to='ai_models/'),
        ),
    ]

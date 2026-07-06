"""
Two-step migration for PatientProfile.emergency_token (unique UUID):
1. Add nullable field
2. Populate existing rows
3. Make non-nullable + unique
"""
import uuid
from django.db import migrations, models


def _populate_tokens(apps, schema_editor):
    PatientProfile = apps.get_model('accounts', 'PatientProfile')
    for p in PatientProfile.objects.filter(emergency_token__isnull=True):
        p.emergency_token = uuid.uuid4()
        p.save(update_fields=['emergency_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
    ]

    operations = [
        # Step 1: add nullable
        migrations.AddField(
            model_name='patientprofile',
            name='emergency_token',
            field=models.UUIDField(null=True, blank=True),
        ),
        # Step 2: populate
        migrations.RunPython(_populate_tokens, migrations.RunPython.noop),
        # Step 3: non-null + unique
        migrations.AlterField(
            model_name='patientprofile',
            name='emergency_token',
            field=models.UUIDField(
                default=uuid.uuid4, unique=True,
                help_text='Token for public emergency card URL',
            ),
        ),
    ]

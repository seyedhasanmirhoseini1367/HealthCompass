"""
Break the ownership chain that could delete patient prediction history.

`AIModel.data_scientist` was CASCADE and the seizure integration selected its
owner with `User.objects.filter(is_staff=True).first()`. An administrative flag
therefore decided data ownership, and deleting that account cascaded:

    staff user -> AIModel -> ModelPrediction -> patient prediction history

reachable through the admin reject action, a bulk delete, or that admin's own
GDPR erasure.

SET_NULL keeps the model and its predictions while still allowing the account to
be erased. PROTECT would have preserved the data by making erasure impossible,
and erasure is not optional.

`is_system` distinguishes "provisioned by the platform" from "the submitter was
erased" — a NULL owner alone cannot express the difference.

Data-safe: adds a nullable column and relaxes a constraint. No row is deleted,
no value is rewritten, and existing owners are untouched.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai_insights', '0005_model_provenance'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='aimodel',
            name='is_system',
            field=models.BooleanField(default=False, help_text='Provisioned by the platform rather than submitted by a data scientist.'),
        ),
        migrations.AlterField(
            model_name='aimodel',
            name='data_scientist',
            field=models.ForeignKey(blank=True, help_text='The data scientist who submitted this model. NULL for system-provisioned models, and for models whose submitter has been erased.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='submitted_models', to=settings.AUTH_USER_MODEL),
        ),
    ]

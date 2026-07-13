from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('medical_records', '0002_add_canonical_value_to_parsedlabvalue'),
    ]

    operations = [
        migrations.AddField(
            model_name='parsedlabvalue',
            name='unit_known',
            field=models.BooleanField(default=True),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rag_assistant', '0005_general_knowledge'),
    ]

    operations = [
        migrations.AddField(
            model_name='querylog',
            name='chart_data',
            field=models.JSONField(
                blank=True, null=True,
                help_text='Chart.js-ready payload for trajectory queries',
            ),
        ),
    ]

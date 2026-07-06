from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rag_assistant', '0003_querylog_add_observability_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='querylog',
            name='safety_routed',
            field=models.BooleanField(
                default=False,
                help_text='True when the safety gate intercepted this query before retrieval',
            ),
        ),
        migrations.AddField(
            model_name='querylog',
            name='triggered_rules',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Guardrail rule names fired on the response',
            ),
        ),
    ]

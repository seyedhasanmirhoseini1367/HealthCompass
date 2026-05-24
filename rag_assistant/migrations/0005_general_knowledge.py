import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rag_assistant', '0004_querylog_impact_measurement'),
    ]

    operations = [
        migrations.CreateModel(
            name='GeneralKnowledgeChunk',
            fields=[
                ('id',          models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('title',       models.CharField(max_length=300)),
                ('source_name', models.CharField(max_length=100)),
                ('source_url',  models.URLField(blank=True, default='')),
                ('topic',       models.CharField(blank=True, default='', max_length=100)),
                ('content',     models.TextField()),
                ('chunk_index', models.IntegerField(default=0)),
                ('embedding',   models.BinaryField(blank=True, null=True)),
                ('metadata',    models.JSONField(blank=True, default=dict)),
                ('created_at',  models.DateTimeField(auto_now_add=True)),
            ],
            options={'indexes': [
                models.Index(fields=['topic'],       name='rag_gkc_topic_idx'),
                models.Index(fields=['source_name'], name='rag_gkc_source_idx'),
            ]},
        ),
        migrations.AddField(
            model_name='querylog',
            name='query_mode',
            field=models.CharField(
                blank=True, default='personal', max_length=20,
                help_text='personal | general | hybrid | emergency',
            ),
        ),
    ]

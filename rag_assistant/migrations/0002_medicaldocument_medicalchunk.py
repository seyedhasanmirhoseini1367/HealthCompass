from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('rag_assistant', '0001_initial'),
        ('medical_records', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MedicalDocument',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)),
                ('title', models.CharField(max_length=300)),
                ('document_type', models.CharField(
                    choices=[('lab_result','Lab Result'),('medication','Medication'),
                             ('wearable','Wearable Data'),('note','Clinical Note'),
                             ('raw_text','Raw Document Text'),('summary','AI Summary')],
                    default='raw_text', max_length=30)),
                ('content', models.TextField()),
                ('source', models.CharField(blank=True, max_length=300)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='rag_documents', to=settings.AUTH_USER_MODEL)),
                ('record', models.ForeignKey(blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='rag_documents', to='medical_records.medicalrecord')),
            ],
            options={'indexes': [
                models.Index(fields=['patient', 'document_type'],
                             name='ragdoc_patient_type_idx'),
                models.Index(fields=['patient', 'created_at'],
                             name='ragdoc_patient_date_idx'),
            ]},
        ),
        migrations.CreateModel(
            name='MedicalChunk',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)),
                ('content', models.TextField()),
                ('chunk_index', models.IntegerField()),
                ('embedding', models.BinaryField(blank=True, null=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='chunks', to='rag_assistant.medicaldocument')),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='rag_chunks', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('document', 'chunk_index')},
                'indexes': [
                    models.Index(fields=['patient'], name='ragchunk_patient_idx'),
                    models.Index(fields=['document', 'chunk_index'],
                                 name='ragchunk_doc_idx'),
                ],
            },
        ),
    ]

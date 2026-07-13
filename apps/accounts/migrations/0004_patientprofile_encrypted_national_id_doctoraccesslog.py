import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.accounts.fields


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_alter_customuser_is_approved'),
    ]

    operations = [
        # Change national_id from a plain CharField to EncryptedCharField (subclass of TextField).
        # Existing plaintext values are returned as-is by the field's from_db_value fallback;
        # new writes are encrypted.  Re-run management command encrypt_legacy_national_ids
        # (not yet written) to encrypt any pre-existing rows in a data migration.
        migrations.AlterField(
            model_name='patientprofile',
            name='national_id',
            field=apps.accounts.fields.EncryptedCharField(
                blank=True,
                help_text=(
                    'Encrypted at rest with a Fernet key derived from SECRET_KEY. '
                    'Stored as opaque ciphertext in a TEXT column; only the '
                    'application can read it. Finnish henkilötunnus — '
                    'treat as highest-sensitivity PII.'
                ),
            ),
        ),
        # Immutable access-audit table for Kanta / GDPR compliance.
        # Never delete rows — archive instead.
        migrations.CreateModel(
            name='DoctorAccessLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('resource', models.CharField(
                    max_length=300,
                    help_text='e.g. "patient_records" or "record:<uuid>"',
                )),
                ('accessed_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(
                    help_text='The doctor (or admin) who performed the access',
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='access_log_entries',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('patient', models.ForeignKey(
                    help_text='The patient whose data was accessed',
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='access_log_received',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-accessed_at'],
                'indexes': [
                    models.Index(fields=['patient', '-accessed_at'], name='accounts_do_patient_b94c45_idx'),
                ],
            },
        ),
    ]

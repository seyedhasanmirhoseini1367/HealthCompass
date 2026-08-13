"""
Preserve who performed each logged access.

DoctorAccessLog.actor is SET_NULL so deleting an account cannot delete audit
rows — but the surviving row then said only that *someone* read this patient's
data. actor_label captures the identity at access time.

The backfill fills it for existing rows from the actor still attached. Rows
whose actor was already deleted stay empty: that information is gone, and
writing a guess into an audit trail would be worse than an honest blank.
"""
from django.db import migrations, models


def backfill_actor_label(apps, schema_editor):
    DoctorAccessLog = apps.get_model('accounts', 'DoctorAccessLog')

    updates = []
    for row in DoctorAccessLog.objects.filter(
            actor__isnull=False, actor_label='').select_related('actor').iterator():
        role = getattr(row.actor, 'role', '') or ''
        row.actor_label = (f'{row.actor.username} ({role})' if role
                           else row.actor.username)
        updates.append(row)

    if updates:
        DoctorAccessLog.objects.bulk_update(updates, ['actor_label'], batch_size=500)


def unbackfill(apps, schema_editor):
    # Reversing only removes the column; nothing else to undo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_normalise_existing_emails'),
    ]

    operations = [
        migrations.AddField(
            model_name='doctoraccesslog',
            name='actor_label',
            field=models.CharField(blank=True, default='', help_text='Username and role of the actor at access time. Survives deletion of the account.', max_length=200),
        ),
        migrations.RunPython(backfill_actor_label, unbackfill),
    ]

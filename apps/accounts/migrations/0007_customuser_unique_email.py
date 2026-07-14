from django.db import migrations, models


def enforce_email_uniqueness(apps, schema_editor):
    """
    Pre-flight check: fail loudly if duplicate non-empty emails exist so the
    operator can resolve them before the UNIQUE constraint is added.

    Then convert empty-string emails to NULL so the constraint allows multiple
    users to have no email (NULL ≠ NULL in SQL).
    """
    User = apps.get_model('accounts', 'CustomUser')
    from django.db.models import Count

    dupes = (
        User.objects
        .values('email')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
        .exclude(email__isnull=True)
        .exclude(email='')
    )
    if dupes.exists():
        emails = [d['email'] for d in dupes]
        raise Exception(
            'Cannot enforce email uniqueness: duplicate emails found: '
            f'{emails}. Resolve manually before running this migration.'
        )

    User.objects.filter(email='').update(email=None)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_patientprofile_emergency_card_enabled_and_more'),
    ]

    operations = [
        migrations.RunPython(enforce_email_uniqueness, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='customuser',
            name='email',
            field=models.EmailField(
                blank=True, max_length=254, null=True,
                unique=True, verbose_name='email address',
            ),
        ),
    ]

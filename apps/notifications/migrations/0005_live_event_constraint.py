"""
At most one live notification event per (subject, dedupe_key).

The dedupe check in `dispatch.event_for_signal` is filter-then-create, which two
concurrent care cycles both pass — producing two events, and two rounds of
delivery to the same caregiver.

Any duplicates that already exist have to be settled before the constraint can
be added, or this migration fails on a database that has been running. The
backfill keeps the NEWEST event of each group live and supersedes the older
ones: the newest carries the current occurrence count and the most recent
deliveries, and superseding is not deleting — every old event keeps its rows,
its count and its delivery history.
"""
from django.conf import settings
from django.db import migrations, models


def settle_existing_duplicates(apps, schema_editor):
    from django.utils import timezone

    NotificationEvent = apps.get_model('notifications', 'NotificationEvent')

    seen = set()
    superseded = []
    # Newest first, so the first row seen for a key is the one that stays live.
    for event in (NotificationEvent.objects
                  .filter(superseded_at__isnull=True)
                  .exclude(dedupe_key='')
                  .order_by('-created_at')
                  .only('id', 'subject_id', 'dedupe_key')):
        key = (event.subject_id, event.dedupe_key)
        if key in seen:
            superseded.append(event.id)
        else:
            seen.add(key)

    if superseded:
        NotificationEvent.objects.filter(id__in=superseded).update(
            superseded_at=timezone.now())


def unsettle(apps, schema_editor):
    """
    Reversing drops the marker, not the events.

    Deliberately a no-op on the data: which event was live before this migration
    ran is not recoverable, and inventing an answer would be worse than leaving
    the column behind for the AddField to remove.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('care', '0006_remove_caretask_medication_statement'),
        ('notifications', '0004_notificationevent_notificationdelivery_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationevent',
            name='superseded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(settle_existing_duplicates, unsettle),
        migrations.AddConstraint(
            model_name='notificationevent',
            constraint=models.UniqueConstraint(condition=models.Q(('superseded_at__isnull', True), models.Q(('dedupe_key', ''), _negated=True)), fields=('subject', 'dedupe_key'), name='one_live_event_per_subject_and_key'),
        ),
    ]

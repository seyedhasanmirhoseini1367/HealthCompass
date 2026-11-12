from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Encounter


@receiver(post_delete, sender=Encounter)
def _delete_audio_file(sender, instance, **kwargs):
    """
    Remove the recording with its row. A signal rather than Encounter.delete()
    so cascades (e.g. deleting the user) and queryset deletes are covered too.
    """
    if instance.audio:
        instance.audio.delete(save=False)

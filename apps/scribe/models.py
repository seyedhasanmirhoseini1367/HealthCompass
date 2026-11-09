"""
Experimental AI scribe: an uploaded consultation recording becomes a transcript
and then a SOAP note. Not for clinical use — see apps/scribe/README.md.
"""
import os
import uuid

from django.conf import settings
from django.db import models


def _audio_upload_to(instance, filename):
    # Random name: the original filename is user-controlled and may contain a
    # patient's name. It is kept (for display only) in original_filename.
    ext = os.path.splitext(filename)[1].lower()
    return f'scribe/audio/{uuid.uuid4().hex}{ext}'


class Encounter(models.Model):
    class Status(models.TextChoices):
        UPLOADED     = 'uploaded',     'Uploaded'
        TRANSCRIBING = 'transcribing', 'Transcribing'
        GENERATING   = 'generating',   'Generating note'
        DONE         = 'done',         'Done'
        FAILED       = 'failed',       'Failed'

    IN_PROGRESS = (Status.UPLOADED, Status.TRANSCRIBING, Status.GENERATING)

    owner             = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                          related_name='scribe_encounters')
    audio             = models.FileField(upload_to=_audio_upload_to, max_length=255)
    original_filename = models.CharField(max_length=255, blank=True)
    status            = models.CharField(max_length=20, choices=Status.choices,
                                         default=Status.UPLOADED)
    error_message     = models.TextField(blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Encounter {self.pk} ({self.status})'

    @property
    def is_in_progress(self):
        return self.status in self.IN_PROGRESS

    @property
    def audio_available(self):
        """
        False when the recording is gone from storage. On a host with ephemeral
        disk (e.g. Render without a persistent disk) every redeploy wipes
        uploads while the database row survives.
        """
        if not self.audio:
            return False
        try:
            return self.audio.storage.exists(self.audio.name)
        except Exception:
            return False


class Transcript(models.Model):
    # One transcript per encounter; OneToOne is a unique FK.
    encounter = models.OneToOneField(Encounter, on_delete=models.CASCADE,
                                     related_name='transcript')
    text      = models.TextField()
    # [{"start": float seconds, "end": float seconds, "text": str}, ...]
    segments  = models.JSONField(default=list)
    language  = models.CharField(max_length=16, blank=True)
    duration  = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Note(models.Model):
    encounter  = models.OneToOneField(Encounter, on_delete=models.CASCADE,
                                      related_name='note')
    # {"subjective": [{"text": str, "source_segments": [int]}], "objective": [...], ...}
    soap       = models.JSONField(default=dict)
    # {"assessment": [[source, ...] per item], "plan": [[...] per item]}
    evidence   = models.JSONField(default=dict)
    raw_output = models.TextField(blank=True)
    provider   = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

from django.db import models


class JobHeartbeat(models.Model):
    """When each scheduled job last started and last succeeded (audit ARCH-03).

    A job that silently stops running looks exactly like a job with nothing to
    do; this row is what tells them apart. /health/ready/ reports stale jobs.
    """
    name           = models.CharField(max_length=100, unique=True)
    last_started   = models.DateTimeField(null=True, blank=True)
    last_succeeded = models.DateTimeField(null=True, blank=True)
    #: Exception type only: messages can quote patient data.
    last_error     = models.CharField(max_length=200, blank=True, default='')
    last_error_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

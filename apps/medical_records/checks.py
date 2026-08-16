"""
Startup checks for the storage that holds patient files.

The problem this exists for
---------------------------
Object storage is opt-in: without OBJECT_STORAGE_URL the project falls back to
MEDIA_ROOT on local disk. On Railway that filesystem is ephemeral, so the
sequence is

    patient uploads a discharge summary
      -> MedicalRecord row is written, file saved to local disk
      -> next deploy replaces the container
      -> the row still exists, the PDF does not

Nothing breaks loudly. The record list still renders, the row still says a file
is attached, and the failure is discovered when somebody clicks Download —
possibly months later, and possibly for a document they no longer have another
copy of.

A silent fallback is the wrong default for medical documents, so this refuses to
let a production deployment start that way. It is a hard error rather than a
warning because a warning in deploy logs is a thing nobody reads.

The escape hatch is deliberate and narrow: ALLOW_EPHEMERAL_MEDIA=True lets an
operator run without durable storage while they set it up, which turns an
accident into a decision someone recorded. That is the whole difference this
check is trying to make.
"""
from django.conf import settings
from django.core.checks import Error, Warning, register

#: Distinct ids so a deployment can silence one without silencing the others.
NO_DURABLE_STORAGE = 'medical_records.E001'
INCOMPLETE_STORAGE = 'medical_records.E002'
EPHEMERAL_ACKED    = 'medical_records.W001'


@register('storage')
def check_medical_file_storage(app_configs, **kwargs):
    """Patient files must survive a restart in production."""
    problems = []

    if settings.DEBUG:
        # Local disk is right for development. The whole point of the check is
        # the deployed case.
        return problems

    url = (getattr(settings, 'OBJECT_STORAGE_URL', '')
           or _configured_object_storage_url())
    acknowledged = bool(getattr(settings, 'ALLOW_EPHEMERAL_MEDIA', False))

    if not url:
        if acknowledged:
            problems.append(Warning(
                'Uploaded medical files are stored on local disk and will be '
                'lost when this container is replaced.',
                hint='ALLOW_EPHEMERAL_MEDIA is set, so this is running by '
                     'explicit choice. Unset it and configure '
                     'OBJECT_STORAGE_URL once durable storage exists.',
                id=EPHEMERAL_ACKED))
            return problems

        problems.append(Error(
            'Medical files would be written to local disk, which does not '
            'survive a deploy. Uploaded documents would disappear while their '
            'database rows remained, so the application would show patients a '
            'record whose file is gone.',
            hint='Set OBJECT_STORAGE_URL, STORAGE_BUCKET_NAME, '
                 'STORAGE_ACCESS_KEY and STORAGE_SECRET_KEY. To run without '
                 'durable storage anyway — accepting that uploads are lost on '
                 'every deploy — set ALLOW_EPHEMERAL_MEDIA=True.',
            id=NO_DURABLE_STORAGE))
        return problems

    # Storage is configured. Credentials being absent is worse than no storage
    # at all: writes fail at upload time, per patient, with the deployment
    # believing it is durable.
    missing = [name for name, value in (
        ('STORAGE_BUCKET_NAME', getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '')),
        ('STORAGE_ACCESS_KEY',  getattr(settings, 'AWS_ACCESS_KEY_ID', '')),
        ('STORAGE_SECRET_KEY',  getattr(settings, 'AWS_SECRET_ACCESS_KEY', '')),
    ) if not value]

    if missing:
        problems.append(Error(
            f'Object storage is configured but unusable: {", ".join(missing)} '
            f'{"is" if len(missing) == 1 else "are"} not set. Uploads would '
            f'fail at the moment a patient tries to save a document.',
            hint='Set the missing variable(s), or unset OBJECT_STORAGE_URL to '
                 'fall back deliberately.',
            id=INCOMPLETE_STORAGE))

    return problems


def _configured_object_storage_url() -> str:
    """
    Whether object storage was actually wired up, read from the storage backend.

    settings.py consumes OBJECT_STORAGE_URL into AWS_S3_ENDPOINT_URL and does
    not keep the original name, so asking for the raw variable alone would
    report "not configured" for a correctly configured deployment.
    """
    return getattr(settings, 'AWS_S3_ENDPOINT_URL', '') or ''

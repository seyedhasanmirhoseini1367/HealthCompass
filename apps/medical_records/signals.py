"""
Erase a medical record's uploaded file when the record is deleted.

Why a signal rather than a service call
---------------------------------------
`purge_user_data` was the only code in the application that deleted a file, and
it is reached by exactly one view. Everything else left the bytes on storage:

    web  record_delete   -> record.delete()          (medical_records/views.py)
    API  record_delete   -> record.delete()          (api/views/records.py)
    admin single delete  -> obj.delete()
    admin bulk delete    -> queryset.delete()        does NOT call Model.delete()
    user deletion        -> cascade                  does NOT call Model.delete()

The last two are the reason this is a signal. `queryset.delete()` and cascades
never call `Model.delete()`, so overriding it — or asking every call site to
remember a service — cannot cover them, and those are precisely the paths a
future contributor is least likely to think about.

Why this is not the careless kind of post_delete
-------------------------------------------------
The dangerous version deletes inside the transaction: if it later rolls back,
the row returns and the bytes are already gone irreversibly. The work is
therefore handed to `transaction.on_commit`, so the file is removed only once
the deletion is durable. Same ordering `purge_user_data` uses, and for the same
reason — file deletion has no compensating action.

Cost, stated honestly: registering a post_delete receiver disables Django's
fast-delete optimisation for MedicalRecord, so deleting a user now loads those
rows instead of issuing one bulk DELETE. That is the price of the signal firing
at all on a cascade, and it is what makes the guarantee real.

Scope is deliberately narrow: this file, on this model. Other uploaded files —
ModelPrediction.input_file, AIModel.model_file, profile pictures — are still
handled only by `purge_user_data`, which is a gap, but not this one's.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_delete, sender='medical_records.MedicalRecord',
          dispatch_uid='medical_records.erase_record_file')
def erase_record_file(sender, instance, **kwargs):
    """Schedule removal of this record's file once the deletion commits."""
    field_file = instance.file
    if not field_file:
        return

    # Captured as plain values now: the row is gone, and the FieldFile's
    # instance must not be relied on by the time the callback runs.
    storage, name = field_file.storage, field_file.name
    record_pk = str(instance.pk)

    def _erase():
        from apps.accounts.services import erase_uploaded_file
        from healthcompass.observability import Event as OpsEvent, emit as ops_emit

        if erase_uploaded_file(storage, name, label=f'record:{record_pk}'):
            return

        # The row is already gone, so nothing in the database still names this
        # file: without a structured event the orphan is unfindable and the
        # failure exists only as prose in a log. purge_user_data emits the same
        # code for the same reason, and this path used to discard the result.
        #
        # Scalars only — the record id and a count, never the filename, which is
        # user-supplied and can carry clinical detail ("HIV-screening.pdf").
        ops_emit(OpsEvent.ERASURE_INCOMPLETE,
                 record_id=record_pk, files_remaining=1, files_total=1)

    transaction.on_commit(_erase)

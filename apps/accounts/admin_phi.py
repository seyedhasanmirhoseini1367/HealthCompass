"""
Make privileged reads of patient data visible in the patient's own trail.

The gap this closes
-------------------
Every carefully built control in this system — consent, `doctor_has_active_link`,
`log_phi_access` — is bypassed by the Django admin, which reaches the ORM
directly. Opening a patient's record there left no trace at all, while the
patient's data export presents "who accessed my records" as a complete answer.
An incomplete answer offered as a complete one is worse than no answer.

What this does NOT do
---------------------
It does not restrict anything. The admin remains the only way to correct a
mis-parsed clinical value — lab values are written by ingestion and two seed
commands, and no view, form, endpoint or command edits them — so removing or
freezing it would take away the only fix for a patient-visible clinical error.
That decision needs a correction workflow behind it, and is deliberately left
open rather than resolved in passing.

So this is the half that costs nothing: the same access, now recorded.

Reads, not listings. Opening one patient's record is an access event; paging a
changelist is not, and logging every row would bury the events that matter in
noise that nobody reads.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PhiAccessLoggedAdmin:
    """
    Mixin for ModelAdmins over patient data.

    Subclasses implement `phi_subject(obj)` to say whose data the row is. A
    subclass that cannot answer that is telling us the model is not
    patient-scoped, and nothing is logged.
    """

    def phi_subject(self, obj):
        """The patient this row belongs to, or None."""
        return getattr(obj, 'patient', None)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        self._record_phi_read(request, object_id)
        return super().change_view(request, object_id, form_url, extra_context)

    def _record_phi_read(self, request, object_id):
        from apps.accounts.authz import log_phi_access

        try:
            obj = self.model.objects.filter(pk=object_id).first()
            if obj is None:
                return
            subject = self.phi_subject(obj)
            if subject is None or getattr(request.user, 'pk', None) == getattr(subject, 'pk', None):
                # A patient opening their own row is not an access event anyone
                # needs to review, and the admin subject is rarely the patient.
                return
            log_phi_access(
                request.user, subject,
                f'admin:{self.model._meta.model_name}:{object_id}')
        except Exception as exc:
            # Never turn a read into a 500 because its logging failed —
            # log_phi_access already emits ACCESS_LOG_FAILED for the storage
            # case, and this guards the lookup itself.
            logger.error('Could not record admin PHI read of %s %s: %s',
                         self.model.__name__, object_id, exc)


class NonEditablePhiAdmin(PhiAccessLoggedAdmin):
    """
    Visible, auditable, deletable — but never rewritten.

    Applied where clinical data has no legitimate hand-editing path:

      * MedicalRecord — the document as ingested. Its metadata can be fixed by
        deleting and re-uploading, which the patient can do themselves; its
        raw_text and parsed_data feed retrieval, so editing them would silently
        change what the assistant answers with no provenance at all.
      * ChatSession / QueryLog — a transcript. Editing a record of what was said
        is not correction, it is falsification, and retention is handled by
        purge_old_query_logs rather than by hand.
      * MedicalDocument / MedicalChunk — derived retrieval artifacts. They are
        rebuilt by indexing, never authored.

    Why deletion stays
    ------------------
    This class first blocked deletion too, and that was wrong. Deletion is not
    the same act as modification: rewriting a record makes it assert something
    its source never said, while deleting it removes the assertion entirely and
    claims nothing. Erasure is also a right the controller has to be able to
    exercise on a subject's behalf, and the admin is the only place a single
    record belonging to the wrong account can be removed.

    It is safe here for a specific reason: `medical_records.signals` erases the
    underlying file on post_delete via transaction.on_commit, so an admin delete
    — including a bulk `delete_selected`, which never calls Model.delete() —
    takes the bytes with it. Blocking deletion did not make anything safer; it
    removed the one erasure path those guarantees were built for.

    ParsedLabValue is the case that needed an alternative rather than a
    prohibition, and it has one: corrections are appended with provenance.

    Addition is blocked everywhere: creating clinical data by hand in the admin
    fabricates a document that no source produced.
    """
    def has_add_permission(self, request):              return False
    def has_change_permission(self, request, obj=None): return False

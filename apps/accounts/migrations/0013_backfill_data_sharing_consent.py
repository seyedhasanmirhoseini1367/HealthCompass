"""
Record DATA_SHARING consent for patients who already approved a doctor.

`ConsentPurpose.DATA_SHARING` was defined and described but checked nowhere, so
revoking "Sharing my records with linked clinicians" changed nothing. It is now
enforced in `authz.doctor_has_active_link`, which every doctor-facing read goes
through.

`has_consent` is default-deny: no row means no consent. Enforcing the check
without this backfill would therefore close every existing doctor link the
moment it deployed — silently, from the doctor's side, with no prompt anywhere
telling the patient what to grant to restore it.

This does NOT invent a consent decision. Every relationship it covers is one the
patient affirmatively approved through `approve_doctor_access`, which sets
status=ACTIVE and stamps `decided_at`. That approval is the act; this records it
against the purpose that now gates it, using the patient's own decision time as
`granted_at` rather than the migration's run time, so the audit trail reflects
when they actually agreed.

Patients with no ACTIVE link get nothing. New approvals grant it in the view.
"""
from django.db import migrations


def backfill_data_sharing(apps, schema_editor):
    Consent = apps.get_model('accounts', 'Consent')
    PatientDoctorRelationship = apps.get_model('accounts', 'PatientDoctorRelationship')

    # Literals, not the model enums: a historical migration must keep working
    # if the choices are renamed later.
    purpose = 'data_sharing'
    version = 'v1'

    approved = (PatientDoctorRelationship.objects
                .filter(status='active')
                .select_related('patient')
                .order_by('decided_at'))

    seen = set()
    for link in approved.iterator():
        patient_id = link.patient_id
        if patient_id in seen or patient_id is None:
            continue
        seen.add(patient_id)

        already = Consent.objects.filter(
            user_id=patient_id, purpose=purpose,
            status='granted', revoked_at__isnull=True, version=version,
        ).exists()
        if already:
            continue

        Consent.objects.create(
            user_id=patient_id,
            purpose=purpose,
            version=version,
            status='granted',
            # The moment the patient approved the link, when known.
            granted_at=link.decided_at or link.created_at,
        )


def unbackfill(apps, schema_editor):
    # Deliberately a no-op. Deleting consent rows would destroy evidence of a
    # decision the patient made, which is the one thing a consent trail exists
    # to preserve. Reversing the migration leaves the rows in place; they are
    # simply no longer consulted once the enforcement is reverted.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_doctoraccesslog_actor_label'),
    ]

    operations = [
        migrations.RunPython(backfill_data_sharing, unbackfill),
    ]

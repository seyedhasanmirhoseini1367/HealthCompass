"""
Sharing rules, shared by the web views and the API (audit CQ-02, SEC-02, SEC-14).

A share is an invitation until the recipient accepts it:

* The patient offers a share to an email address. If an account has that
  address, a PENDING grant is created and the recipient is notified in-app.
  Nothing is visible to them yet: `authz.sharing_grant` honours ACTIVE only.
* The recipient accepts (ACTIVE) or declines (the row is removed).
* The response to the patient is identical whether or not the address belongs
  to an account, so the form cannot be used to probe who is registered.

Exception: when the recipient has already asked to see the patient's data (an
open ShareRequest), answering that request activates the grant directly; the
recipient's request is their acceptance.
"""
from django.db import transaction
from django.utils import timezone

from .authz import SHARE_SCOPES, can_create_grant, can_revoke_grant

NEUTRAL_OFFER_MESSAGE = ('If {email} has a HealthCompass account, they have been invited. '
                         'Nothing is shared until they accept, and you can withdraw it at any time.')
NEUTRAL_REQUEST_MESSAGE = ('If {email} has a HealthCompass account, your request was sent. '
                           'Nothing is shared unless they approve.')
SCOPE_LABELS = {'records': 'records', 'alerts': 'care alerts', 'appointments': 'appointments'}


class SharingError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def clean_scopes(values) -> list[str]:
    return [v for v in (values or []) if v in SHARE_SCOPES]


def scope_labels(scopes) -> str:
    return ', '.join(SCOPE_LABELS.get(s, s) for s in scopes)


def find_account_by_email(identifier: str):
    """Email only, exact and case-insensitive. Usernames never match (SEC-02)."""
    from .models import CustomUser

    identifier = (identifier or '').strip().lower()
    if '@' not in identifier:
        return None
    return CustomUser.objects.filter(email__iexact=identifier).order_by('pk').first()


def _name(user) -> str:
    return user.get_full_name() or user.username


def _notify(user, title, message, link='/care/#sharing'):
    from apps.notifications.models import Notification

    Notification.objects.create(user=user, type=Notification.Type.SYSTEM,
                                title=title, message=message, link=link)


def _log(patient, action, recipient_id):
    from .models import DoctorAccessLog

    DoctorAccessLog.objects.create(actor=patient, patient=patient,
                                   resource=f'{action}:{recipient_id}')


def apply_grant(patient, recipient, scopes, *, activate: bool):
    """Create or update the patient's grant to *recipient* with exactly *scopes*.

    activate=False leaves a new or previously stopped grant PENDING until the
    recipient accepts; an already ACTIVE grant keeps its status and only its
    scopes change (the recipient accepted once already).
    """
    from .models import SharingGrant

    flags = {'can_view_records': 'records' in scopes,
             'can_view_alerts': 'alerts' in scopes,
             'can_view_appointments': 'appointments' in scopes}
    with transaction.atomic():
        grant = (SharingGrant.objects.select_for_update()
                 .filter(patient=patient, recipient=recipient).first())
        if grant is None:
            grant = SharingGrant.objects.create(
                patient=patient, recipient=recipient,
                status=SharingGrant.Status.ACTIVE if activate else SharingGrant.Status.PENDING,
                **flags)
        else:
            for field, value in flags.items():
                setattr(grant, field, value)
            if activate:
                grant.status = SharingGrant.Status.ACTIVE
            elif grant.status != SharingGrant.Status.ACTIVE:
                grant.status = SharingGrant.Status.PENDING  # re-invite after a stop
            grant.revoked_at = None
            grant.revoked_by = None
            grant.revoke_reason = ''
            grant.save()
    _log(patient, 'share_granted' if grant.status == SharingGrant.Status.ACTIVE else 'share_offered',
         recipient.pk)
    return grant


def offer_share(patient, email: str, scopes) -> str:
    """The patient shares with an email address. Returns the neutral message to
    show whatever happened; raises SharingError only for the patient's own
    input mistakes (no email, no scopes, their own address)."""
    from .models import ShareRequest, SharingGrant

    email = (email or '').strip().lower()
    scopes = clean_scopes(scopes)
    if not email:
        raise SharingError('Enter the email address of the person to share with.', 'missing_email')
    if not scopes:
        raise SharingError('Choose at least one thing to share.', 'missing_scopes')
    if email == (patient.email or '').lower():
        raise SharingError('You already have access to your own records.', 'self')
    if not can_create_grant(patient, patient):
        raise SharingError('Not permitted.', 'forbidden')

    recipient = find_account_by_email(email)
    if recipient is not None and recipient.pk != patient.pk:
        open_request = ShareRequest.objects.filter(
            requester=recipient, patient=patient, status=ShareRequest.Status.PENDING)
        asked = open_request.exists()
        grant = apply_grant(patient, recipient, scopes, activate=asked)
        if asked:
            open_request.update(status=ShareRequest.Status.APPROVED, responded_at=timezone.now())
            _notify(recipient, f'{_name(patient)} is now sharing with you',
                    f'You can see their {scope_labels(scopes)}.', f'/care/person/{patient.pk}/')
        elif grant.status == SharingGrant.Status.PENDING:
            _notify(recipient, f'{_name(patient)} wants to share their health information with you',
                    f'They offered their {scope_labels(scopes)}. Accept or decline on Companions.')
            _mail_invitation(patient, recipient, scopes)
    return NEUTRAL_OFFER_MESSAGE.format(email=email)


def _pending_for(recipient, pk):
    from .models import SharingGrant

    grant = SharingGrant.objects.filter(pk=pk, recipient=recipient,
                                        status=SharingGrant.Status.PENDING).first()
    if grant is None:
        raise SharingError('This invitation is no longer open.', 'not_found')
    return grant


def accept_share(recipient, pk):
    from .models import SharingGrant

    grant = _pending_for(recipient, pk)
    grant.status = SharingGrant.Status.ACTIVE
    grant.save(update_fields=['status'])
    _log(grant.patient, 'share_granted', recipient.pk)
    _notify(grant.patient, f'{_name(recipient)} accepted your invitation',
            'They can now see what you chose to share. You can stop this at any time.')
    return grant


def decline_share(recipient, pk):
    """Declining removes the invitation; the patient is not told (saying no
    should cost nothing), and the pending row simply disappears from their list."""
    grant = _pending_for(recipient, pk)
    patient = grant.patient
    grant.delete()
    _log(patient, 'share_declined', recipient.pk)


def revoke_share(actor, grant, reason: str = ''):
    """Stop sharing (patient) or withdraw an invitation. Raises SharingError."""
    if not can_revoke_grant(actor, grant):
        raise SharingError('Not permitted.', 'forbidden')
    grant.revoke(by=actor, reason=reason)
    from .models import DoctorAccessLog
    DoctorAccessLog.objects.create(actor=actor, patient=grant.patient,
                                   resource=f'share_revoked:{grant.recipient_id}')
    return grant



def _mail_invitation(patient, recipient, scopes):
    """Email the recipient too: an in-app notice is invisible until they sign in."""
    import logging

    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    ctx = {'recipient': recipient, 'sharer': _name(patient), 'scopes': scope_labels(scopes),
           'link': f"{settings.ACCOUNT_DEFAULT_HTTP_PROTOCOL}://{settings.SITE_DOMAIN}/care/#sharing"}
    try:
        msg = EmailMultiAlternatives(f"{ctx['sharer']} wants to share their health information with you",
                                     render_to_string('accounts/email/share_invitation.txt', ctx),
                                     settings.DEFAULT_FROM_EMAIL, [recipient.email])
        msg.attach_alternative(render_to_string('accounts/email/share_invitation.html', ctx), 'text/html')
        msg.send()
    except Exception as exc:
        logging.getLogger(__name__).warning('Share invitation mail failed: %s', type(exc).__name__)

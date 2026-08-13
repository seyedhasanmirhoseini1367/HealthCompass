"""
Consent endpoints for the mobile client.

Always scoped to request.user — there is no path to read or change another
user's consent, by id or otherwise. The mobile app needs the same three
capabilities as the web page: see current status, grant a purpose, withdraw a
purpose.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.consent import (consent_history, consent_status,
                                   grant_consent, revoke_consent)
from apps.accounts.models import ConsentPurpose


def _serialize(rows):
    return [{
        'purpose':         r['purpose'],
        'label':           r['label'],
        'description':     r['description'],
        'granted':         r['granted'],
        'required':        r['required'],
        'current_version': r['current_version'],
        'granted_version': r['granted_version'],
        'granted_at':      r['granted_at'].isoformat() if r['granted_at'] else None,
        'needs_reconsent': r['needs_reconsent'],
    } for r in rows]


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def consent_list(request):
    """Current consent state for every purpose, for the authenticated user."""
    return Response({'consents': _serialize(consent_status(request.user))})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def consent_grant(request):
    purpose = (request.data.get('purpose') or '').strip()
    if purpose not in ConsentPurpose.values:
        return Response({'error': 'Unknown consent purpose.'},
                        status=status.HTTP_400_BAD_REQUEST)
    grant_consent(request.user, purpose)
    return Response({'consents': _serialize(consent_status(request.user))})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def consent_revoke(request):
    purpose = (request.data.get('purpose') or '').strip()
    if purpose not in ConsentPurpose.values:
        return Response({'error': 'Unknown consent purpose.'},
                        status=status.HTTP_400_BAD_REQUEST)
    revoked = revoke_consent(request.user, purpose)
    return Response({
        'revoked':  revoked,
        'consents': _serialize(consent_status(request.user)),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def consent_history_view(request):
    """Append-only trail of the caller's own consent decisions."""
    return Response({'history': [{
        'purpose':    c.purpose,
        'version':    c.version,
        'status':     c.status,
        'granted_at': c.granted_at.isoformat() if c.granted_at else None,
        'revoked_at': c.revoked_at.isoformat() if c.revoked_at else None,
    } for c in consent_history(request.user)]})

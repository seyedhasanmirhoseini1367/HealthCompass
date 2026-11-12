"""Emergency card: the patient view and the public paramedic view."""
import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django_ratelimit.decorators import ratelimit

from healthcompass.net import client_ip, hash_ip, ratelimit_ip_key

from ..models import (PatientProfile, EmergencyCardView)

logger = logging.getLogger(__name__)


# ── Emergency card ────────────────────────────────────────────────────────────

def _get_client_ip(request) -> str:
    """The client address: X-Forwarded-For read from the right, trusting only
    TRUSTED_PROXY_COUNT hops (healthcompass.net.client_ip). Its left-most entry
    is client-controlled and is never used."""
    return client_ip(request)

@login_required
def emergency_card(request):
    """Patient's own emergency card view with QR code and recent access summary."""
    import base64, io
    import qrcode
    from django.conf import settings as s

    profile, _ = PatientProfile.objects.get_or_create(user=request.user)
    site_url   = getattr(s, 'SITE_URL', request.build_absolute_uri('/').rstrip('/'))
    public_url = f'{site_url}/accounts/emergency/{profile.emergency_token}/'

    qr = qrcode.QRCode(box_size=5, border=3,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(public_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    since_30d    = timezone.now() - timedelta(days=30)
    recent_views = EmergencyCardView.objects.filter(
        profile=profile, viewed_at__gte=since_30d
    ).count()

    return render(request, 'accounts/emergency_card.html', {
        'profile':      profile,
        'qr_b64':       qr_b64,
        'public_url':   public_url,
        'recent_views': recent_views,
    })


@ratelimit(key=ratelimit_ip_key, rate='30/m', method=ratelimit.ALL, block=False)
def emergency_card_public(request, token):
    """No-login public emergency card — scannable by paramedics or doctors.

    Rate-limited (30 req/min per client IP) and logged to EmergencyCardView.
    Returns 404 if the patient has disabled their card.
    """
    if getattr(request, 'limited', False):
        response = HttpResponse('Too many requests. Please wait a minute.', status=429)
        response['Retry-After'] = '60'
        return response
    # HMAC, not a bare SHA-256: an IPv4 hash is reversible by enumeration.
    ip_hash = hash_ip(_get_client_ip(request))

    profile = get_object_or_404(PatientProfile, emergency_token=token)
    if not profile.emergency_card_enabled:
        raise Http404

    EmergencyCardView.objects.create(profile=profile, ip_hash=ip_hash)

    return render(request, 'accounts/emergency_card_public.html', {
        'profile': profile,
        'patient': profile.user,
    })


@login_required
def revoke_emergency_token(request):
    """POST only: regenerate the emergency card token, invalidating old links."""
    if request.method == 'POST':
        profile, _ = PatientProfile.objects.get_or_create(user=request.user)
        profile.regenerate_emergency_token()
        messages.success(request, 'Emergency card link revoked. A new link has been generated.')
    return redirect('accounts:emergency_card')


@login_required
def toggle_emergency_card(request):
    """POST only: flip emergency_card_enabled on the patient's profile."""
    if request.method == 'POST':
        profile, _ = PatientProfile.objects.get_or_create(user=request.user)
        profile.emergency_card_enabled = not profile.emergency_card_enabled
        profile.save(update_fields=['emergency_card_enabled'])
        state = 'enabled' if profile.emergency_card_enabled else 'disabled'
        messages.success(request, f'Emergency card {state}.')
    return redirect('accounts:emergency_card')

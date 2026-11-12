import logging

from django.conf import settings

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts import auth_service

from ..serializers import UserSerializer, RegisterSerializer
from ..throttling import (AUTH_THROTTLES, PasswordResetThrottle, RegisterThrottle,
                          UploadThrottle)

logger = logging.getLogger(__name__)

User = get_user_model()


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes(AUTH_THROTTLES)
def login(request):
    identifier = str(request.data.get('email', '')).strip()
    password   = str(request.data.get('password', ''))

    if not identifier or not password:
        return Response({'error': 'Email and password are required.'},
                        status=status.HTTP_400_BAD_REQUEST)

    from apps.accounts import login_throttle
    if login_throttle.is_locked(identifier):
        response = Response({'error': login_throttle.MESSAGE, 'code': 'rate_limited'},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        response['Retry-After'] = str(login_throttle.WINDOW_SECONDS)
        return response

    # Same rules and wording as the web login (auth_service). The backend runs
    # the password hasher when no account matches, so timing reveals nothing.
    try:
        auth_user = auth_service.authenticate_identifier(request, identifier, password)
        login_throttle.clear(identifier)
    except auth_service.AuthError as exc:
        if exc.code == 'invalid_credentials':
            login_throttle.record_failure(identifier)
        code = (status.HTTP_401_UNAUTHORIZED if exc.code == 'invalid_credentials'
                else status.HTTP_403_FORBIDDEN)
        return Response({'error': exc.message, 'code': exc.code}, status=code)

    refresh = RefreshToken.for_user(auth_user)
    return Response({
        'access':  str(refresh.access_token),
        'refresh': str(refresh),
        'user':    UserSerializer(auth_user).data,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegisterThrottle])
def register(request):
    serializer = RegisterSerializer(data=request.data, context={'request': request})
    if auth_service.verification_required():
        # Identical answer for new and taken addresses (SEC-14); tokens come
        # from auth/login/ once the address is confirmed or the user signs in.
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        try:
            auth_service.register_or_notify(data['email'], data['password'],
                                            data.get('first_name', ''), data.get('last_name', ''),
                                            request=request)
        except auth_service.AuthError as exc:
            if exc.code != 'email_taken':
                return Response({'error': exc.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': auth_service.REGISTER_DONE_MESSAGE.format(email=data['email'])},
                        status=status.HTTP_202_ACCEPTED)
    if serializer.is_valid():
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response({
            'user':    UserSerializer(user).data,
            'access':  str(refresh.access_token),
            'refresh': str(refresh),
        }, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def forgot_password(request):
    email = request.data.get('email', '').strip().lower()
    if not email:
        return Response({'error': 'Email is required.'}, status=status.HTTP_400_BAD_REQUEST)
    # Lookup and send happen off the request, so the response time does not
    # depend on whether the address has an account (audit SEC-14).
    _run_detached(_send_password_reset, email, request.build_absolute_uri('/'),
                  request.is_secure() or not settings.DEBUG)
    return Response({'detail': 'If that email is registered, a reset link has been sent.'})


def _send_password_reset(email, base_url, use_https):
    from urllib.parse import urlparse

    from django.contrib.auth.forms import PasswordResetForm
    try:
        if not User.objects.filter(email__iexact=email).exists():
            return
        form = PasswordResetForm({'email': email})
        if form.is_valid():
            host = urlparse(base_url).netloc or settings.SITE_DOMAIN
            form.save(domain_override=host, use_https=use_https,
                      email_template_name='accounts/email/password_reset_email.txt',
                      html_email_template_name='accounts/email/password_reset_email.html',
                      subject_template_name='accounts/email/password_reset_subject.txt')
    except Exception as exc:
        logger.warning('Password reset mail failed: %s', type(exc).__name__)
    finally:
        if not getattr(settings, 'RAG_AUTO_INDEX_SYNC', False):
            from django.db import connection
            connection.close()


def _run_detached(func, *args):
    """Run in a daemon thread; inline under the test runner."""
    if getattr(settings, 'RAG_AUTO_INDEX_SYNC', False):
        func(*args)
        return
    import threading
    threading.Thread(target=func, args=args, daemon=True).start()


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(UserSerializer(request.user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def register_fcm_token(request):
    token = request.data.get('token', '').strip()
    if not token:
        return Response({'error': 'Token required.'}, status=status.HTTP_400_BAD_REQUEST)
    from apps.notifications.models import FCMDevice
    FCMDevice.objects.update_or_create(token=token, defaults={'user': request.user})
    return Response({'detail': 'Token registered.'})


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def profile_update(request):
    from ..serializers import ProfileUpdateSerializer
    serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    serializer.save()
    return Response(UserSerializer(request.user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
def profile_picture_upload(request):
    from apps.medical_records.services import validate_image_upload

    pic = request.FILES.get('profile_picture')
    if not pic:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

    # Validated by magic bytes, not by the client-declared Content-Type: that
    # header is attacker-controlled, and an SVG announced as image/png used to
    # be stored verbatim and then served from our own origin.
    ok, message = validate_image_upload(pic)
    if not ok:
        return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)

    user = request.user
    if user.profile_picture:
        try:
            user.profile_picture.delete(save=False)
        except Exception as exc:
            logger.warning('Could not delete the old profile picture: %s', type(exc).__name__)
    user.profile_picture = pic
    user.save(update_fields=['profile_picture'])
    return Response(UserSerializer(user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes(AUTH_THROTTLES)
def change_password(request):
    old_pw = request.data.get('old_password', '')
    new_pw = request.data.get('new_password', '')
    if not old_pw or not new_pw:
        return Response({'error': 'Both fields required.'}, status=status.HTTP_400_BAD_REQUEST)
    # Password validators and token revocation are shared with the web.
    try:
        auth_service.change_password(request.user, old_pw, new_pw)
    except auth_service.AuthError as exc:
        return Response({'error': exc.message}, status=status.HTTP_400_BAD_REQUEST)
    except DjangoValidationError as exc:
        return Response({'error': ' '.join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    refresh = RefreshToken.for_user(request.user)
    return Response({'access': str(refresh.access_token), 'refresh': str(refresh)})


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def emergency_card(request):
    from apps.accounts.models import PatientProfile
    profile, _ = PatientProfile.objects.get_or_create(user=request.user)
    if request.method == 'PATCH':
        # The web form's validation (blood type choices, lengths, phone format),
        # applied to the stored values overlaid with the fields sent.
        from apps.accounts.forms import PatientProfileForm
        fields = PatientProfileForm.Meta.fields
        merged = {f: getattr(profile, f) for f in fields}
        merged.update({f: (request.data.get(f) or '') for f in fields if f in request.data})
        form = PatientProfileForm(merged, instance=profile)
        if not form.is_valid():
            return Response(form.errors.get_json_data(), status=status.HTTP_400_BAD_REQUEST)
        form.save()
    return Response({
        'full_name':               request.user.get_full_name() or request.user.username,
        'email':                   request.user.email,
        'date_of_birth':           str(request.user.date_of_birth) if request.user.date_of_birth else None,
        'phone_number':            request.user.phone_number,
        'blood_type':              profile.blood_type,
        'allergies':               profile.allergies,
        'emergency_contact_name':  profile.emergency_contact_name,
        'emergency_contact_phone': profile.emergency_contact_phone,
        'token':                   str(profile.emergency_token),
    })


# ── Sign-out and token revocation (audit SEC-04) ─────────────────────────────

def _drop_fcm_device(user, fcm_token):
    if not fcm_token:
        return
    from apps.notifications.models import FCMDevice
    FCMDevice.objects.filter(user=user, token=str(fcm_token)).delete()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    """Blacklist the supplied refresh token and forget this device's push token."""
    from rest_framework_simplejwt.exceptions import TokenError

    raw = request.data.get('refresh', '')
    if not raw:
        return Response({'error': 'refresh is required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        token = RefreshToken(raw)
        if str(token.get('user_id')) != str(request.user.pk):
            return Response({'error': 'Token does not belong to this account.'},
                            status=status.HTTP_400_BAD_REQUEST)
        token.blacklist()
    except TokenError:
        pass  # already invalid: the outcome the caller wanted
    _drop_fcm_device(request.user, request.data.get('fcm_token'))
    return Response(status=status.HTTP_205_RESET_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_all(request):
    """Blacklist every refresh token this account holds, on every device."""
    revoked = auth_service.revoke_all_tokens(request.user)
    from apps.notifications.models import FCMDevice
    FCMDevice.objects.filter(user=request.user).delete()
    return Response({'revoked': revoked})


# ── Account deletion (audit SEC-13) ──────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([PasswordResetThrottle])
def account_delete_request(request):
    """For accounts without a password: email a signed, 30-minute deletion
    token (and a web link). The token is then sent to DELETE auth/account/."""
    from apps.accounts.security_views import send_delete_link
    send_delete_link(request, request.user)
    return Response({'detail': f'A confirmation link was sent to {request.user.email}.'})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
@throttle_classes(AUTH_THROTTLES)
def account_delete(request):
    """Delete the caller's account. Requires the password, or for accounts
    without one, the token from auth/account/delete-request/."""
    from apps.accounts.security_views import read_delete_token
    from apps.accounts.services import purge_user_data

    user = request.user
    if user.has_usable_password():
        if not user.check_password(str(request.data.get('password', ''))):
            return Response({'error': 'Incorrect password.'}, status=status.HTTP_403_FORBIDDEN)
    else:
        owner = read_delete_token(str(request.data.get('token', '')))
        if owner is None or owner.pk != user.pk:
            return Response({'error': 'Invalid or expired deletion token.'},
                            status=status.HTTP_403_FORBIDDEN)
    auth_service.revoke_all_tokens(user)
    purge_user_data(user)
    return Response(status=status.HTTP_204_NO_CONTENT)

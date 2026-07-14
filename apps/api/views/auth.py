from django.contrib.auth import authenticate, get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from ..serializers import UserSerializer, RegisterSerializer

User = get_user_model()


@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    identifier = request.data.get('email', '').strip()
    password   = request.data.get('password', '')

    if not identifier or not password:
        return Response({'error': 'Email and password are required.'},
                        status=status.HTTP_400_BAD_REQUEST)

    user = (User.objects.filter(email__iexact=identifier).first() or
            User.objects.filter(username__iexact=identifier).first())

    if not user:
        return Response({'error': 'Invalid email or password.'},
                        status=status.HTTP_401_UNAUTHORIZED)

    auth_user = authenticate(username=user.username, password=password)
    if not auth_user:
        return Response({'error': 'Invalid email or password.'},
                        status=status.HTTP_401_UNAUTHORIZED)

    if not auth_user.is_active:
        return Response({'error': 'Account is inactive.'},
                        status=status.HTTP_403_FORBIDDEN)

    refresh = RefreshToken.for_user(auth_user)
    return Response({
        'access':  str(refresh.access_token),
        'refresh': str(refresh),
        'user':    UserSerializer(auth_user).data,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def register(request):
    serializer = RegisterSerializer(data=request.data)
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
def forgot_password(request):
    email = request.data.get('email', '').strip().lower()
    if not email:
        return Response({'error': 'Email is required.'}, status=status.HTTP_400_BAD_REQUEST)
    if User.objects.filter(email__iexact=email).exists():
        try:
            from django.contrib.auth.forms import PasswordResetForm
            form = PasswordResetForm({'email': email})
            if form.is_valid():
                form.save(request=request, use_https=True,
                          email_template_name='accounts/email/password_reset_email.txt',
                          subject_template_name='accounts/email/password_reset_subject.txt')
        except Exception:
            pass
    return Response({'detail': 'If that email is registered, a reset link has been sent.'})


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
    user = request.user
    for field in ['first_name', 'last_name', 'phone_number', 'date_of_birth']:
        if field in request.data:
            setattr(user, field, request.data[field] or '')
    user.save()
    return Response(UserSerializer(user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def profile_picture_upload(request):
    pic = request.FILES.get('profile_picture')
    if not pic:
        return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)
    allowed = ('image/jpeg', 'image/png', 'image/webp', 'image/gif')
    if pic.content_type not in allowed:
        return Response({'error': 'Only JPEG, PNG, WebP, or GIF images are accepted.'},
                        status=status.HTTP_400_BAD_REQUEST)
    user = request.user
    if user.profile_picture:
        try:
            user.profile_picture.delete(save=False)
        except Exception:
            pass
    user.profile_picture = pic
    user.save(update_fields=['profile_picture'])
    return Response(UserSerializer(user).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password(request):
    old_pw = request.data.get('old_password', '')
    new_pw = request.data.get('new_password', '')
    if not old_pw or not new_pw:
        return Response({'error': 'Both fields required.'}, status=status.HTTP_400_BAD_REQUEST)
    if not request.user.check_password(old_pw):
        return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)
    if len(new_pw) < 8:
        return Response({'error': 'New password must be at least 8 characters.'}, status=status.HTTP_400_BAD_REQUEST)
    request.user.set_password(new_pw)
    request.user.save()
    refresh = RefreshToken.for_user(request.user)
    return Response({'access': str(refresh.access_token), 'refresh': str(refresh)})


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def emergency_card(request):
    from apps.accounts.models import PatientProfile
    profile, _ = PatientProfile.objects.get_or_create(user=request.user)
    if request.method == 'PATCH':
        for field in ['blood_type', 'allergies', 'emergency_contact_name', 'emergency_contact_phone']:
            if field in request.data:
                setattr(profile, field, request.data[field] or '')
        profile.save()
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

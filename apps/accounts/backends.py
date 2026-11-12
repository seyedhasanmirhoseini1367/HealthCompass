from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()

        # Email first, username only when no account has that email. A username
        # may look like an email (accounts created before '@' was rejected), and
        # matching both at once let such a username shadow the real owner of the
        # address (audit SEC-02). order_by('pk') keeps .first() deterministic.
        identifier = (username or '').strip()
        user = (UserModel.objects.filter(email__iexact=identifier).order_by('pk').first()
                or UserModel.objects.filter(username__iexact=identifier).order_by('pk').first())

        if user is None:
            # Constant-time defence: always run the password hasher so that
            # "user not found" and "wrong password" take the same wall-clock
            # time, preventing email/username enumeration via timing.
            UserModel().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

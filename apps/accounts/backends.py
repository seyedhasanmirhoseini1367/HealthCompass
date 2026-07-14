from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import Q


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()

        # Single query: match on email OR username (case-insensitive).
        # .first() returns None instead of raising MultipleObjectsReturned —
        # safe now that email has a unique constraint, and safe as a fallback
        # even if a data anomaly somehow produced a duplicate.
        user = UserModel.objects.filter(
            Q(email__iexact=username) | Q(username__iexact=username)
        ).first()

        if user is None:
            # Constant-time defence: always run the password hasher so that
            # "user not found" and "wrong password" take the same wall-clock
            # time, preventing email/username enumeration via timing.
            UserModel().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

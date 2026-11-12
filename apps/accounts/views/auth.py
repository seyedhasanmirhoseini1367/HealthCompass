"""Sign-up, sign-in, sign-out and Google sign-in."""
import logging

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.views import PasswordResetView
from django.conf import settings
from django.shortcuts import render, redirect
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from allauth.socialaccount.views import SignupView as BaseSocialSignupView
from django_ratelimit.decorators import ratelimit

from healthcompass.net import ratelimit_ip_key

from ..forms import (RegisterForm, LoginForm)

logger = logging.getLogger(__name__)


def _email_is_verified(user, email: str) -> bool:
    """
    Has this account proven it owns this address?

    allauth records confirmation in EmailAddress.verified. A local account with
    no EmailAddress row, or one that is unconfirmed, has proven nothing — see
    the pre-hijack note in AutoCompleteSocialSignup.get().

    Fails CLOSED: if allauth's model cannot be read for any reason, the answer
    is "not verified". An error here must not become a free account link.
    """
    try:
        from allauth.account.models import EmailAddress
        return EmailAddress.objects.filter(
            user=user, email__iexact=email, verified=True).exists()
    except Exception:
        logger.exception('Could not determine email verification state; '
                         'treating as unverified')
        return False


class AutoCompleteSocialSignup(BaseSocialSignupView):
    """Skip the signup form — auto-complete without asking for a role.
    If the email already exists, connect the Google account to that user.
    Role defaults to patient; admin can change it later."""

    def get(self, request, *args, **kwargs):
        from django.contrib.auth import get_user_model
        from allauth.socialaccount.internal.flows.signup import (
            complete_social_signup, clear_pending_signup,
        )

        sociallogin = self.sociallogin
        User = get_user_model()

        # Case 1: email already registered — connect Google to that account,
        # but ONLY if that account has proven it owns the address.
        #
        # Account pre-hijack, which this guard closes:
        #   1. Attacker registers locally as victim@gmail.com with a password
        #      they choose. ACCOUNT_EMAIL_VERIFICATION is 'none', so no
        #      confirmation mail is ever sent and the victim never learns.
        #   2. Victim later signs in with Google. allauth matches the address
        #      and, unguarded, connects the victim's Google identity to the
        #      ATTACKER's account.
        #   3. Victim uploads medical records into an account the attacker can
        #      still log into with the original password.
        #
        # Google's assertion proves the VICTIM owns the mailbox; an unverified
        # local account has proven nothing. So an unverified match is the
        # suspicious party and must not receive the identity. Refusing degrades
        # to a denial of service against the squatted address — recoverable via
        # password reset, which itself proves ownership — instead of a silent
        # account takeover.
        if sociallogin.email_addresses:
            email = sociallogin.email_addresses[0].email
            try:
                existing_user = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                existing_user = None
            except User.MultipleObjectsReturned:
                # Case-variant duplicates can exist because email uniqueness is
                # case-sensitive while lookup is not. Refuse rather than pick.
                logger.error('Social login matched multiple accounts for one address')
                messages.error(request, 'Could not complete Google sign-in. '
                                        'Please contact support.')
                return redirect('/accounts/login/')

            if existing_user is not None:
                if not _email_is_verified(existing_user, email):
                    logger.warning(
                        'Refused to connect a social identity to an unverified '
                        'local account (user pk=%s)', existing_user.pk)
                    messages.error(
                        request,
                        'An account with this email already exists but the address '
                        'has not been confirmed, so it cannot be linked to Google. '
                        'Sign in with your password, or use “Forgot password” to '
                        'confirm you own this address.')
                    return redirect('/accounts/login/')

                clear_pending_signup(request)
                sociallogin.connect(request, existing_user)
                return complete_social_signup(request, sociallogin)

        # Case 2: brand new user — create account as patient
        try:
            from allauth.socialaccount.adapter import get_adapter
            clear_pending_signup(request)
            get_adapter().save_user(request, sociallogin, form=None)
            return complete_social_signup(request, sociallogin)
        except Exception as e:
            logger.warning('Social auto-signup failed: %s', e)
            messages.error(request, 'Could not complete Google sign-in. Please try again.')
            return redirect('/accounts/login/')


class SafePasswordResetView(PasswordResetView):
    """Wraps Django's PasswordResetView — always redirects to done page even if email fails.

    Rate limited by IP: each POST sends an email, so an unthrottled endpoint lets
    anyone use the app as a spam relay and probe which addresses are registered.
    """
    template_name = 'accounts/password_reset.html'
    email_template_name = 'accounts/email/password_reset_email.txt'
    html_email_template_name = 'accounts/email/password_reset_email.html'
    subject_template_name = 'accounts/email/password_reset_subject.txt'
    success_url = reverse_lazy('accounts:password_reset_done')

    # block=False rather than block=True: django-ratelimit's blocking mode raises
    # Ratelimited, a PermissionDenied subclass, which Django renders as 403.
    # Rate limiting should say 429 so clients can back off correctly.
    @method_decorator(
        ratelimit(key=ratelimit_ip_key, rate=settings.RATELIMIT_PASSWORD_RESET, method='POST', block=False)
    )
    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            return render(request, self.template_name,
                          {'form': self.get_form()}, status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except Exception as e:
            logger.error('Password reset email failed: %s', e)
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(self.get_success_url())


@ratelimit(key=ratelimit_ip_key, rate=settings.RATELIMIT_REGISTER, method='POST', block=False)
def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    if getattr(request, 'limited', False):
        messages.error(request, "Too many sign-up attempts. Please try again later.")
        return render(request, "accounts/register.html", {"form": RegisterForm()}, status=429)
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            from .. import auth_service
            if auth_service.verification_required():
                # Same page whether or not the address was taken (SEC-14). The
                # new account signs in after following the link.
                email = form.cleaned_data['email']
                try:
                    auth_service.register_or_notify(
                        email, form.cleaned_data['password1'],
                        form.cleaned_data.get('first_name', ''),
                        form.cleaned_data.get('last_name', ''),
                        username=form.cleaned_data['username'], request=request)
                except auth_service.AuthError as exc:
                    if exc.code != 'email_taken':
                        form.add_error(None, exc.message)
                        return render(request, "accounts/register.html", {"form": form})
                return render(request, "accounts/register_done.html", {
                    'message': auth_service.REGISTER_DONE_MESSAGE.format(email=email)})
            try:
                user = auth_service.register_patient(
                    form.cleaned_data['email'], form.cleaned_data['password1'],
                    form.cleaned_data.get('first_name', ''), form.cleaned_data.get('last_name', ''),
                    username=form.cleaned_data['username'], request=request)
            except auth_service.AuthError as exc:
                form.add_error(None, exc.message)
                messages.error(request, "Please correct the errors below.")
                return render(request, "accounts/register.html", {"form": form})

            # Three backends are configured, so login() cannot infer which one
            # authenticated this user and raises ValueError if simply handed the
            # object. Re-authenticating with the credentials just submitted is
            # the backend-safe route: it walks AUTHENTICATION_BACKENDS, sets
            # user.backend, and still enforces every backend check (including
            # user_can_authenticate) rather than asserting a login that the
            # auth stack never actually approved.
            auth_user = authenticate(
                request,
                username=user.username,
                password=form.cleaned_data["password1"],
            )
            if auth_user is None:
                # Account exists but could not be authenticated — send them to
                # the normal login flow rather than failing the whole request.
                logger.warning('Post-registration authenticate() failed for %s', user.pk)
                messages.success(request, "Your account was created. Please log in.")
                return redirect("accounts:login")

            login(request, auth_user)
            messages.success(request, f"Welcome to HealthCompass, {auth_user.get_full_name() or auth_user.username}!")
            return redirect("dashboard:home")
        messages.error(request, "Please correct the errors below.")
    else:
        form = RegisterForm()
    return render(request, "accounts/register.html", {"form": form})


@ratelimit(key=ratelimit_ip_key, rate=settings.RATELIMIT_LOGIN, method='POST', block=False)
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard:home")
    # Keyed by IP rather than username: an attacker controls the username field,
    # so a per-username limit is trivially sidestepped and also lets one attacker
    # lock a known victim out of their own account.
    if getattr(request, 'limited', False):
        messages.error(request, "Too many login attempts. Please wait a minute and try again.")
        response = render(request, "accounts/login.html", {"form": LoginForm(request)}, status=429)
        response['Retry-After'] = '60'
        return response
    if request.method == "POST":
        from .. import login_throttle
        identifier = request.POST.get('username', '')
        # Per-account limit on top of the per-IP one: many addresses guessing
        # one account. Same response whether or not the account exists.
        if login_throttle.is_locked(identifier):
            messages.error(request, login_throttle.MESSAGE)
            response = render(request, "accounts/login.html", {"form": LoginForm(request)}, status=429)
            response['Retry-After'] = str(login_throttle.WINDOW_SECONDS)
            return response
        form = LoginForm(request, data=request.POST)
        if not form.is_valid():
            login_throttle.record_failure(identifier)
        else:
            login_throttle.clear(identifier)
            from .. import auth_service
            user = form.get_user()
            try:
                auth_service.check_login_allowed(user)
            except auth_service.AuthError as exc:
                messages.warning(request, exc.message)
                return redirect("accounts:login")
            next_url = request.GET.get("next", "")
            from django.utils.http import url_has_allowed_host_and_scheme
            if not (next_url and url_has_allowed_host_and_scheme(
                    next_url, allowed_hosts={request.get_host()})):
                next_url = reverse("dashboard:home")
            messages.success(request, f"Welcome back, {user.first_name or user.username}!")
            # allauth's login stages: asks for the TOTP code when the account
            # has an authenticator. Email verification is not a login gate here;
            # unverified users sign in and see a banner instead (SEC-05).
            from allauth.account.utils import perform_login
            return perform_login(request, user, email_verification='none', redirect_url=next_url)
        messages.error(request, "Invalid username or password.")
    else:
        form = LoginForm(request)
    return render(request, "accounts/login.html", {"form": form})


@require_POST
def logout_view(request):
    """POST only: a GET link let any page sign a user out with an <img> tag."""
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("home")

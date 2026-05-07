from django.shortcuts import redirect


class RoleSelectionMiddleware:
    """
    Redirect newly registered social-auth users to the role-selection page
    before they can access anything else. The flag is set in save_user() and
    cleared once the user submits the select_role form.
    """
    EXEMPT_PREFIXES = (
        '/accounts/select-role/',
        '/accounts/logout/',
        '/static/',
        '/media/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and request.session.get('needs_role_selection')
            and not any(request.path.startswith(p) for p in self.EXEMPT_PREFIXES)
        ):
            return redirect('accounts:select_role')
        return self.get_response(request)

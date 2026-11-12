from django.shortcuts import redirect, render

from apps.dashboard.navigation import home_url


def home(request):
    # The landing page is for visitors. A signed-in user's "home" is their own
    # start page (My Health, My Patients, Workspace, ...), so the logo and the
    # bare address take them there instead of back to the marketing page.
    if request.user.is_authenticated:
        return redirect(home_url(request.user))
    return render(request, 'home.html', {})

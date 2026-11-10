"""
Sign people out after a period of inactivity.

Django's default is a session that lasts two weeks whatever happens, which is
long for a site holding health records on shared or lost devices. Here the
session lasts SESSION_COOKIE_AGE (2 hours by default, SESSION_IDLE_MINUTES in
the environment) from the last request, so someone actively using the site is
never signed out mid-task.

Refreshing the expiry means saving the session. Doing that on every request
(SESSION_SAVE_EVERY_REQUEST) would write to the database for each status poll
the Scribe and notification badge make. Instead the session is touched at most
once per TOUCH_SECONDS, which moves the expiry forward just the same.
"""
import time

TOUCH_SECONDS = 300
KEY = '_idle_touch'


class IdleSessionMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = getattr(request, 'session', None)
        user = getattr(request, 'user', None)
        if session is not None and user is not None and user.is_authenticated:
            now = int(time.time())
            if now - session.get(KEY, 0) >= TOUCH_SECONDS:
                # Any change marks the session modified, so it is saved with a
                # fresh expiry of SESSION_COOKIE_AGE from now.
                session[KEY] = now
        return self.get_response(request)

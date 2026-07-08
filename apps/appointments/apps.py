import sys
from django.apps import AppConfig


class AppointmentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.appointments'
    label = 'appointments'

    def ready(self):
        # Don't start scheduler in management commands (migrate, collectstatic, etc.)
        if any(cmd in sys.argv for cmd in ('migrate', 'makemigrations', 'collectstatic',
                                            'shell', 'test', 'check', 'dbshell')):
            return
        self._start_reminder_scheduler()

    @staticmethod
    def _start_reminder_scheduler():
        import threading

        def _run():
            try:
                from django.core.management import call_command
                call_command('send_appointment_reminders', verbosity=0)
            except Exception:
                pass
            timer = threading.Timer(600, _run)   # every 10 minutes
            timer.daemon = True
            timer.start()

        # First run 90 seconds after startup to let DB settle
        t = threading.Timer(90, _run)
        t.daemon = True
        t.start()

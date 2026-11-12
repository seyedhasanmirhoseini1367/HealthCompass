"""
P0.5 — production must not put patient files on a disk that disappears.

The failure being prevented is quiet, which is what makes it dangerous:

    upload -> row written, file on local disk
    deploy -> container replaced
    later  -> the row still says a file is attached, and it is not

Nothing raises at the time. The record list renders, the attachment is listed,
and the loss surfaces when somebody clicks Download — possibly months on, for a
document they may have no other copy of.

These tests are about the check firing at the right times, and — just as
important — not firing in development, where local disk is correct.
"""
from django.core.checks import Error, Warning
from django.test import SimpleTestCase, override_settings

from apps.medical_records.checks import (EPHEMERAL_ACKED, INCOMPLETE_STORAGE,
                                         NO_DURABLE_STORAGE,
                                         check_medical_file_storage)


def _ids(problems):
    return {p.id for p in problems}


class DevelopmentTests(SimpleTestCase):

    @override_settings(DEBUG=True)
    def test_local_disk_is_fine_in_development(self):
        """The check must not nag during ordinary local work."""
        self.assertEqual(check_medical_file_storage(None), [])


class ProductionTests(SimpleTestCase):

    @override_settings(DEBUG=False, AWS_S3_ENDPOINT_URL='',
                       ALLOW_EPHEMERAL_MEDIA=False)
    def test_production_without_object_storage_is_an_error(self):
        """ACCEPTANCE — the deploy must stop, not warn."""
        problems = check_medical_file_storage(None)

        self.assertIn(NO_DURABLE_STORAGE, _ids(problems))
        self.assertTrue(any(isinstance(p, Error) for p in problems))

    @override_settings(DEBUG=False, AWS_S3_ENDPOINT_URL='',
                       ALLOW_EPHEMERAL_MEDIA=False)
    def test_the_error_says_what_to_set(self):
        """A blocking error with no remedy is just an outage."""
        problem = check_medical_file_storage(None)[0]

        for name in ('OBJECT_STORAGE_URL', 'STORAGE_BUCKET_NAME',
                     'STORAGE_ACCESS_KEY', 'STORAGE_SECRET_KEY'):
            self.assertIn(name, problem.hint)

    @override_settings(DEBUG=False, AWS_S3_ENDPOINT_URL='',
                       ALLOW_EPHEMERAL_MEDIA=True)
    def test_an_operator_can_acknowledge_the_risk_explicitly(self):
        """
        The escape hatch turns an accident into a recorded decision — which is
        the entire difference the check is trying to make.
        """
        problems = check_medical_file_storage(None)

        self.assertIn(EPHEMERAL_ACKED, _ids(problems))
        self.assertFalse(any(isinstance(p, Error) for p in problems))
        self.assertTrue(any(isinstance(p, Warning) for p in problems))

    @override_settings(DEBUG=False, AWS_S3_ENDPOINT_URL='https://r2.example',
                       AWS_STORAGE_BUCKET_NAME='hc-media',
                       AWS_ACCESS_KEY_ID='key', AWS_SECRET_ACCESS_KEY='secret')
    def test_a_complete_configuration_passes(self):
        self.assertEqual(check_medical_file_storage(None), [])

    @override_settings(DEBUG=False, AWS_S3_ENDPOINT_URL='https://r2.example',
                       AWS_STORAGE_BUCKET_NAME='hc-media',
                       AWS_ACCESS_KEY_ID='', AWS_SECRET_ACCESS_KEY='secret')
    def test_configured_but_credential_less_storage_is_an_error(self):
        """
        ACCEPTANCE — worse than no storage, because the deployment believes it
        is durable while every upload fails at the moment a patient saves.
        """
        problems = check_medical_file_storage(None)

        self.assertIn(INCOMPLETE_STORAGE, _ids(problems))
        self.assertIn('STORAGE_ACCESS_KEY', problems[0].msg)

    @override_settings(DEBUG=False, AWS_S3_ENDPOINT_URL='https://r2.example',
                       AWS_STORAGE_BUCKET_NAME='', AWS_ACCESS_KEY_ID='',
                       AWS_SECRET_ACCESS_KEY='')
    def test_every_missing_credential_is_named(self):
        """Fixing one at a time across three deploys is nobody's afternoon."""
        message = check_medical_file_storage(None)[0].msg

        for name in ('STORAGE_BUCKET_NAME', 'STORAGE_ACCESS_KEY',
                     'STORAGE_SECRET_KEY'):
            self.assertIn(name, message)


class RegistrationTests(SimpleTestCase):

    def test_the_check_is_registered_with_django(self):
        """An unregistered check is a function nobody calls."""
        from django.core.checks import registry

        registered = {c.__name__ for c in registry.registry.get_checks()}
        self.assertIn('check_medical_file_storage', registered)

    def test_the_current_test_environment_is_clean(self):
        """`manage.py check` must stay quiet for the suite itself."""
        from django.core.management import call_command

        call_command('check')          # raises SystemCheckError on failure

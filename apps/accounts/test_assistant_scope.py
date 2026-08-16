"""
Who the assistant may be asked about.

Answering a question about someone sends their record excerpts to an external
LLM, so this is an egress decision, not a viewing decision — and it is stricter
than the care page on purpose:

  * the RECORDS scope, not `alerts`. "Tell me if something is wrong" is a
    promise about notifications, not permission to read a file aloud to a
    language model.
  * the SUBJECT's own EXTERNAL_LLM consent. The data is theirs, so the
    agreement that matters is theirs. A caregiver consenting for themselves
    does not consent for their father.

The second is the one most easily got wrong, because the obvious implementation
checks the person clicking the button.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.authz import assistant_subjects, can_ask_assistant_about
from apps.accounts.consent import grant_consent, revoke_consent
from apps.accounts.models import ConsentPurpose, SharingGrant

User = get_user_model()


class _Scope(TestCase):

    def setUp(self):
        self.child = User.objects.create_user(
            'as_child', email='as_child@test.invalid', password='pw',
            role='patient', first_name='Maria')
        self.parent = User.objects.create_user(
            'as_parent', email='as_parent@test.invalid', password='pw',
            role='patient', first_name='Anna')
        self.stranger = User.objects.create_user(
            'as_stranger', email='as_str@test.invalid', password='pw',
            role='patient')

    def _grant(self, **kwargs):
        options = dict(can_view_records=True, status=SharingGrant.Status.ACTIVE)
        options.update(kwargs)
        return SharingGrant.objects.create(
            patient=self.parent, recipient=self.child, **options)

    def _consent(self, user):
        grant_consent(user, ConsentPurpose.EXTERNAL_LLM)


class OwnDataTests(_Scope):

    def test_anyone_may_ask_about_themselves(self):
        self.assertTrue(can_ask_assistant_about(self.child, self.child))

    def test_an_anonymous_visitor_may_ask_about_nobody(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(can_ask_assistant_about(AnonymousUser(), self.parent))

    def test_the_subject_list_always_starts_with_yourself(self):
        self.assertEqual(assistant_subjects(self.child), [self.child])


class SharedDataTests(_Scope):

    def test_a_records_grant_plus_subject_consent_allows_it(self):
        self._grant()
        self._consent(self.parent)

        self.assertTrue(can_ask_assistant_about(self.child, self.parent))
        self.assertIn(self.parent, assistant_subjects(self.child))

    def test_an_alerts_only_grant_is_not_enough(self):
        """
        ACCEPTANCE — "tell me if something is wrong" is not "read her file to
        an AI". These are different promises and the scopes keep them apart.
        """
        self._grant(can_view_records=False, can_view_alerts=True)
        self._consent(self.parent)

        self.assertFalse(can_ask_assistant_about(self.child, self.parent))
        self.assertNotIn(self.parent, assistant_subjects(self.child))

    def test_the_subjects_consent_is_required_not_the_askers(self):
        """
        ACCEPTANCE — the mistake the obvious implementation makes.

        The child consents for herself; the records being transmitted are her
        mother's, and her mother has agreed to nothing.
        """
        self._grant()
        self._consent(self.child)          # the asker, not the subject

        self.assertFalse(can_ask_assistant_about(self.child, self.parent))

    def test_revoking_the_subjects_consent_takes_effect_immediately(self):
        self._grant()
        self._consent(self.parent)
        self.assertTrue(can_ask_assistant_about(self.child, self.parent))

        revoke_consent(self.parent, ConsentPurpose.EXTERNAL_LLM)

        self.assertFalse(can_ask_assistant_about(self.child, self.parent))

    def test_a_revoked_grant_ends_it(self):
        grant = self._grant()
        self._consent(self.parent)
        grant.revoke(by=self.parent, reason='no longer needed')

        self.assertFalse(can_ask_assistant_about(self.child, self.parent))

    def test_an_expired_grant_ends_it(self):
        self._grant(expires_at=timezone.now() - timedelta(days=1))
        self._consent(self.parent)

        self.assertFalse(can_ask_assistant_about(self.child, self.parent))

    def test_a_stranger_is_refused_even_with_consent_on_file(self):
        self._consent(self.parent)

        self.assertFalse(can_ask_assistant_about(self.stranger, self.parent))

    def test_the_grant_does_not_work_in_reverse(self):
        """Anna sharing with Maria does not let Anna read Maria's records."""
        self._grant()
        self._consent(self.child)

        self.assertFalse(can_ask_assistant_about(self.parent, self.child))


class ConsistencyTests(_Scope):
    """The offered list and the enforced check must never disagree."""

    def test_every_offered_subject_passes_the_predicate(self):
        self._grant()
        self._consent(self.parent)

        for subject in assistant_subjects(self.child):
            self.assertTrue(can_ask_assistant_about(self.child, subject),
                            f'{subject} was offered but is not permitted')

    def test_a_subject_that_fails_the_predicate_is_never_offered(self):
        self._grant()          # no consent from the parent

        subjects = assistant_subjects(self.child)
        self.assertEqual(subjects, [self.child])

    def test_an_administrator_gains_nothing(self):
        """
        Administrative authority is not caregiving authority. This mirrors
        can_create_grant, which has no administrative branch either.
        """
        admin = User.objects.create_superuser(
            'as_admin', email='as_admin@test.invalid', password='pw-admin-1')
        self._consent(self.parent)

        self.assertFalse(can_ask_assistant_about(admin, self.parent))
        self.assertEqual(assistant_subjects(admin), [admin])

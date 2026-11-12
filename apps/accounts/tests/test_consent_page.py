"""
REGRESSION — the "turning this back on restores access" warning on the consent page.

Withdrawing DATA_SHARING closes every clinician link at once without ending the
links themselves, so switching it back on restores all of them together. That
consequence was stated only on the approval screen, which a patient re-enabling
from Privacy & Consent never sees — and re-enabling is the moment several people
silently regain access.

The block counting those links is one `dictsort` away from being silently wrong.
`regroup` collapses only ADJACENT equal keys, and `PatientDoctorRelationship`
declares no `Meta.ordering`, so without the sort the active links arrive
interleaved with pending and revoked ones and split into several one-element
groups. The warning would then render once per active link, each claiming
"1 clinician" — a wrong number, no exception, nothing red.

So these tests assert on the rendered page rather than on a helper: they fail if
the `dictsort` is dropped, which a test of a Python function could not do.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.consent import grant_consent, revoke_consent
from apps.accounts.models import ConsentPurpose, PatientDoctorRelationship

User = get_user_model()
Status = PatientDoctorRelationship.Status
SHARING = ConsentPurpose.DATA_SHARING

#: Marks the warning block. Counting occurrences is what catches the split-group
#: failure: without the sort there is one block per active link, not one in total.
#:
#: Deliberately a phrase from the rendered sentence, not the CSS class — the
#: class also appears three times in the page's <style> block, so matching on it
#: would report the warning as present on every page including the ones where it
#: correctly does not render.
BLOCK = 'restores record access'


class _Page(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'cp_patient', email='cp_patient@test.invalid', password='pw', role='patient')
        self.client.force_login(self.patient)

    def _doctor(self, username, first, last):
        return User.objects.create_user(
            username, email=f'{username}@test.invalid', password='pw',
            role='doctor', first_name=first, last_name=last)

    def _link(self, doctor, status):
        return PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=doctor, status=status)

    def _render(self):
        response = self.client.get(reverse('accounts:consent'))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class InterleavedLinksTests(_Page):
    """
    Active links deliberately separated by a pending and a revoked one, which is
    the arrangement that splits the group when the sort is missing.
    """

    def setUp(self):
        super().setUp()
        self.aalto = self._doctor('cp_a', 'Anna', 'Aalto')
        self.berg = self._doctor('cp_b', 'Bo', 'Berg')
        self.cruz = self._doctor('cp_c', 'Carla', 'Cruz')
        self.waiting = self._doctor('cp_p', 'Pia', 'Pending')
        self.dismissed = self._doctor('cp_r', 'Rolf', 'Revoked')

        # Creation order is the order the queryset returns without an ordering.
        self._link(self.aalto, Status.ACTIVE)
        self._link(self.waiting, Status.PENDING)
        self._link(self.berg, Status.ACTIVE)
        self._link(self.dismissed, Status.REVOKED)
        self._link(self.cruz, Status.ACTIVE)

        grant_consent(self.patient, SHARING)
        revoke_consent(self.patient, SHARING)

    def test_the_interleaving_is_real(self):
        """
        Guards the guard. If these rows ever arrive already grouped by status,
        the tests below pass with or without the dictsort and stop protecting
        anything — this fails first and says why.

        Uses `.all()`, the same query the template makes, because the query
        SHAPE decides the answer here: `values_list('status')` is covered by the
        composite (patient, status) index and comes back status-sorted, while
        fetching whole objects uses the patient_id index and comes back in
        insertion order. Asserting on the cheaper query would have quietly
        measured a different plan than the one that matters.
        """
        statuses = [link.status for link in self.patient.my_doctors.all()]
        self.assertNotEqual(statuses, sorted(statuses),
                            'links are no longer interleaved; the dictsort tests '
                            'below have stopped testing anything')

    def test_exactly_one_warning_block_is_rendered(self):
        """ACCEPTANCE — without the dictsort this renders three."""
        self.assertEqual(self._render().count(BLOCK), 1)

    def test_the_count_is_the_number_of_active_links(self):
        """ACCEPTANCE — without the dictsort each block claims 1 clinician."""
        self.assertIn('3 clinicians', self._render())

    def test_every_active_clinician_is_named(self):
        body = self._render()
        for doctor in (self.aalto, self.berg, self.cruz):
            with self.subTest(doctor=doctor.username):
                self.assertIn(doctor.get_full_name(), body)

    def test_a_revoked_links_clinician_is_not_named(self):
        self.assertNotIn(self.dismissed.get_full_name(), self._render())

    def test_a_pending_links_clinician_is_not_named(self):
        self.assertNotIn(self.waiting.get_full_name(), self._render())

    def test_the_warning_says_what_it_restores(self):
        body = self._render()
        self.assertIn('restores record access', body)
        self.assertIn('still active', body)


class NothingToRestoreTests(_Page):
    """The warning must not appear when it would be untrue or meaningless."""

    def test_nothing_renders_while_sharing_is_granted(self):
        doctor = self._doctor('cp_g', 'Gil', 'Grant')
        self._link(doctor, Status.ACTIVE)
        grant_consent(self.patient, SHARING)

        self.assertNotIn(BLOCK, self._render())

    def test_nothing_renders_when_no_link_is_active(self):
        self._link(self._doctor('cp_p2', 'Pia', 'Pending'), Status.PENDING)
        self._link(self._doctor('cp_r2', 'Rolf', 'Revoked'), Status.REVOKED)
        grant_consent(self.patient, SHARING)
        revoke_consent(self.patient, SHARING)

        self.assertNotIn(BLOCK, self._render())

    def test_nothing_renders_when_there_are_no_links_at_all(self):
        grant_consent(self.patient, SHARING)
        revoke_consent(self.patient, SHARING)

        self.assertNotIn(BLOCK, self._render())

    def test_nothing_renders_for_a_patient_who_never_decided(self):
        """Not granted, but also never withdrawn — and no clinicians either."""
        self.assertNotIn(BLOCK, self._render())


class SingleLinkTests(_Page):
    """Pluralisation is part of the sentence being correct."""

    def test_one_active_link_reads_as_singular(self):
        self._link(self._doctor('cp_s', 'Sam', 'Solo'), Status.ACTIVE)
        grant_consent(self.patient, SHARING)
        revoke_consent(self.patient, SHARING)

        body = self._render()
        self.assertIn('1 clinician', body)
        self.assertNotIn('1 clinicians', body)


class OtherPatientsLinksTests(_Page):
    """The count is the viewer's own, never anybody else's."""

    def test_another_patients_active_links_are_not_counted(self):
        stranger = User.objects.create_user(
            'cp_other', email='cp_other@test.invalid', password='pw', role='patient')
        theirs = self._doctor('cp_theirs', 'Nils', 'Nyman')
        PatientDoctorRelationship.objects.create(
            patient=stranger, doctor=theirs, status=Status.ACTIVE)

        mine = self._doctor('cp_mine', 'Mika', 'Mine')
        self._link(mine, Status.ACTIVE)
        grant_consent(self.patient, SHARING)
        revoke_consent(self.patient, SHARING)

        body = self._render()
        self.assertIn('1 clinician', body)
        self.assertIn(mine.get_full_name(), body)
        self.assertNotIn(theirs.get_full_name(), body)

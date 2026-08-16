"""
The dashboard: what it says, what it refuses to say, and whose data it uses.

Three properties are load-bearing.

  1. Absence is never good news. A patient with nothing recorded must not see a
     green tick — NO_DATA and OK are different answers and the difference is the
     whole point of having a state model.
  2. Nothing is invented. No health score, no adherence percentage, no composite
     index. A number on a health dashboard is read as a clinical judgement.
  3. The caregiver summary carries no clinical content. It says whether someone
     needs attention; their page, behind their scopes, says why.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import SharingGrant
from apps.ai_insights.models import HealthAlert
from apps.appointments.models import Appointment
from apps.care.models import CareTask, MonitoringSignal, TaskOccurrence
from apps.dashboard.overview import build_dashboard
from apps.dashboard.state import State

User = get_user_model()

DRUG = 'Zidovudine'


class _Dash(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'db_patient', email='db_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.other = User.objects.create_user(
            'db_other', email='db_other@test.invalid', password='pw',
            role='patient', first_name='Onni')
        # Midday today, not "now".
        #
        # The fixtures build occurrences as `anchor - N hours`, and the
        # dashboard counts what is due TODAY. Anchored to the current time,
        # `anchor - 2h` falls on yesterday whenever the suite runs between
        # midnight and 02:00 — so these tests passed all day and failed at
        # night, which is the worst possible schedule for noticing.
        #
        # Midday is far enough from both boundaries that every offset the
        # fixtures use stays on the day the test means.
        self.anchor = timezone.localtime().replace(
            hour=12, minute=0, second=0, microsecond=0)

    def _task(self, label=DRUG):
        return CareTask.objects.create(
            patient=self.patient, label=label, times_of_day=['08:00'])

    def _occurrence(self, state=None, hours_ago=2, task=None, patient=None):
        occurrence = TaskOccurrence.objects.create(
            task=task or self._task(), patient=patient or self.patient,
            due_at=self.anchor - timedelta(hours=hours_ago))
        if state:
            occurrence.state = state
            occurrence.save(update_fields=['state'])
        return occurrence

    def _grant(self, patient, recipient, **kwargs):
        options = dict(can_view_alerts=True, status=SharingGrant.Status.ACTIVE)
        options.update(kwargs)
        return SharingGrant.objects.create(
            patient=patient, recipient=recipient, **options)


class EmptyStateTests(_Dash):
    """A brand-new account is the state most easily got wrong."""

    def test_a_patient_with_nothing_is_not_told_everything_is_fine(self):
        """ACCEPTANCE — NO_DATA is not OK, and must never render as reassurance."""
        data = build_dashboard(self.patient)

        self.assertFalse(data['has_any_data'])
        self.assertEqual(data['attention'], [])
        for section in data['sections'].values():
            self.assertIn(section.state, (State.NO_DATA, State.UNAVAILABLE),
                          f'{section.key} claimed a state it has no data for')

    def test_the_page_renders_for_an_empty_account(self):
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Nothing needs your attention right now')

    def test_no_medication_reminders_says_so_rather_than_showing_zero(self):
        section = build_dashboard(self.patient)['sections']['medications']

        self.assertEqual(section.state, State.NO_DATA)
        self.assertIn('No medication reminders', section.summary)

    def test_no_appointments_says_so(self):
        section = build_dashboard(self.patient)['sections']['appointments']

        self.assertEqual(section.state, State.NO_DATA)
        self.assertIn('No upcoming appointments', section.summary)


class NoInventedNumbersTests(_Dash):

    def test_there_is_no_health_score_anywhere(self):
        """ACCEPTANCE — no defensible basis exists for one, so none is shown."""
        self._occurrence(TaskOccurrence.State.CONFIRMED)
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')

        body = response.content.decode().lower()
        for term in ('health score', 'wellness score', 'risk index',
                     'adherence rate', '% adherence'):
            self.assertNotIn(term, body)

    def test_medications_are_counted_not_scored(self):
        task = self._task()
        self._occurrence(TaskOccurrence.State.CONFIRMED, task=task, hours_ago=3)
        self._occurrence(TaskOccurrence.State.UNCONFIRMED, task=task, hours_ago=4)

        section = build_dashboard(self.patient)['sections']['medications']
        self.assertIn('1 of 2 confirmed today', section.summary)


class SilenceWordingTests(_Dash):
    """The care rule, restated where a patient will actually read it."""

    def test_an_unconfirmed_dose_is_described_as_no_answer(self):
        """ACCEPTANCE — the dashboard must not assert a dose was missed."""
        self._occurrence(TaskOccurrence.State.UNCONFIRMED)

        section = build_dashboard(self.patient)['sections']['medications']
        item = next(i for i in section.items if i.state == State.ATTENTION)

        self.assertIn('not confirmed', item.title)
        self.assertIn('did not hear back', item.detail)

    def test_the_rendered_page_never_says_the_patient_missed_a_dose(self):
        self._occurrence(TaskOccurrence.State.UNCONFIRMED)
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode().lower()

        self.assertNotIn('you missed', body)
        self.assertNotIn('did not take', body)


class StateSemanticsTests(_Dash):

    def test_only_a_clinical_alert_reaches_urgent(self):
        """
        ACCEPTANCE — URGENT is reserved for a rule a clinician authored. A
        pattern this app inferred from button presses is ATTENTION at most.
        """
        self._occurrence(TaskOccurrence.State.UNCONFIRMED)
        MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=self.anchor, window_end=self.anchor,
            subject_key='task:x')

        data = build_dashboard(self.patient)
        self.assertNotEqual(data['overall'], State.URGENT)

    def test_a_critical_health_alert_is_urgent(self):
        HealthAlert.objects.create(
            patient=self.patient, severity=HealthAlert.Severity.CRITICAL,
            title='Critical potassium', message='…')

        data = build_dashboard(self.patient)
        self.assertEqual(data['sections']['alerts'].state, State.URGENT)
        self.assertEqual(data['overall'], State.URGENT)

    def test_a_failing_subsystem_is_unavailable_not_ok(self):
        """
        A section that could not be computed must not look like good news.

        Previously driven by making clinical_summary raise. That service was
        deleted, and deleting it took the section's only try/except with it —
        a failure in this one card then raised out of build_dashboard and
        returned a 500 for the entire dashboard. This test caught that, so it
        now drives the remaining query instead: the guarantee is the state
        mapping, not which call happened to fail.
        """
        from unittest.mock import patch

        with patch('apps.dashboard.overview.recent_measurements',
                   side_effect=RuntimeError('boom')):
            data = build_dashboard(self.patient)

        self.assertEqual(data['sections']['care'].state, State.UNAVAILABLE)

    def test_one_failing_section_does_not_take_the_page_down(self):
        """
        The consequence the above protects against, asserted end to end: the
        dashboard still renders, and still renders for a patient who has data
        in the sections that did not fail.
        """
        from unittest.mock import patch

        HealthAlert.objects.create(
            patient=self.patient, severity=HealthAlert.Severity.CRITICAL,
            title='Critical potassium', message='…')
        self.client.force_login(self.patient)

        with patch('apps.dashboard.overview.recent_measurements',
                   side_effect=RuntimeError('boom')):
            response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Critical potassium')

    def test_attention_never_contains_a_non_actionable_state(self):
        Appointment.objects.create(
            patient=self.patient, title='Cardiology',
            appointment_datetime=timezone.now() + timedelta(days=2))

        data = build_dashboard(self.patient)
        for _, item in data['attention']:
            self.assertIn(item.state, State.ACTIONABLE)

    def test_an_upcoming_appointment_is_not_an_attention_item(self):
        Appointment.objects.create(
            patient=self.patient, title='Cardiology',
            appointment_datetime=timezone.now() + timedelta(days=2))

        data = build_dashboard(self.patient)
        self.assertEqual(data['attention'], [])
        self.assertEqual(data['sections']['appointments'].state, State.UPCOMING)


class IsolationTests(_Dash):

    def test_another_patients_data_never_appears(self):
        CareTask.objects.create(patient=self.other, label='Warfarin',
                                times_of_day=['08:00'])
        Appointment.objects.create(
            patient=self.other, title='Their appointment',
            appointment_datetime=timezone.now() + timedelta(days=1))
        HealthAlert.objects.create(patient=self.other, title='Their alert',
                                   message='…')

        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')

        self.assertNotContains(response, 'Warfarin')
        self.assertNotContains(response, 'Their appointment')
        self.assertNotContains(response, 'Their alert')

    def test_the_dashboard_takes_no_patient_parameter(self):
        """ACCEPTANCE — there is no id in the request to substitute."""
        self.client.force_login(self.patient)
        response = self.client.get(f'/dashboard/?patient={self.other.pk}')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Onni')


class CaregiverDashboardTests(_Dash):
    """Caregiver is a relationship, not a role — so it is the same dashboard."""

    def test_someone_with_no_grants_sees_no_watching_section(self):
        data = build_dashboard(self.patient)

        self.assertFalse(data['is_caregiver'])
        self.assertEqual(data['watching'].items, [])

    def test_a_caregiver_sees_the_person_they_watch(self):
        self._grant(self.other, self.patient)

        data = build_dashboard(self.patient)
        self.assertTrue(data['is_caregiver'])
        self.assertEqual([i.title for i in data['watching'].items], ['Onni'])

    def test_a_revoked_grant_removes_them(self):
        grant = self._grant(self.other, self.patient)
        grant.revoke(by=self.other, reason='no longer needed')

        self.assertFalse(build_dashboard(self.patient)['is_caregiver'])

    def test_an_expired_grant_removes_them(self):
        self._grant(self.other, self.patient,
                    expires_at=timezone.now() - timedelta(days=1))

        self.assertFalse(build_dashboard(self.patient)['is_caregiver'])

    def test_a_records_only_grant_does_not_put_them_on_the_dashboard(self):
        """Care watching rides the alerts scope, not records."""
        self._grant(self.other, self.patient,
                    can_view_alerts=False, can_view_records=True)

        self.assertFalse(build_dashboard(self.patient)['is_caregiver'])

    def test_someone_needing_attention_is_flagged(self):
        self._grant(self.other, self.patient)
        MonitoringSignal.objects.create(
            patient=self.other,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=self.anchor, window_end=self.anchor,
            subject_key='task:y')

        data = build_dashboard(self.patient)
        item = data['watching'].items[0]

        self.assertEqual(item.state, State.ATTENTION)
        self.assertEqual(item.detail, '1 thing needs attention')

    def test_the_summary_carries_no_clinical_detail(self):
        """
        ACCEPTANCE — the dashboard says WHO needs attention, never what about.

        A drug name here would hand a diagnosis to anyone glancing at the
        screen, and the caregiver has to open the person's page — where the
        patient's scopes are enforced — to learn anything more.
        """
        self._grant(self.other, self.patient)
        task = CareTask.objects.create(patient=self.other, label=DRUG,
                                       times_of_day=['08:00'])
        occurrence = self._occurrence(TaskOccurrence.State.UNCONFIRMED,
                                      task=task, patient=self.other)
        signal = MonitoringSignal.objects.create(
            patient=self.other,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=self.anchor, window_end=self.anchor,
            subject_key=f'task:{task.pk}')
        signal.occurrences.set([occurrence])

        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')

        self.assertContains(response, 'Onni')
        self.assertNotContains(response, DRUG)

    def test_no_recorded_activity_is_shown_as_unknown_not_fine(self):
        """Sharing exists, nothing has happened. That is not 'they are well'."""
        self._grant(self.other, self.patient)

        item = build_dashboard(self.patient)['watching'].items[0]
        self.assertEqual(item.state, State.NO_DATA)

    def test_people_needing_attention_sort_first(self):
        third = User.objects.create_user(
            'db_third', email='db_third@test.invalid', password='pw',
            role='patient', first_name='Kaisa')
        self._grant(self.other, self.patient)
        self._grant(third, self.patient)
        MonitoringSignal.objects.create(
            patient=third, kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=self.anchor, window_end=self.anchor, subject_key='t')

        items = build_dashboard(self.patient)['watching'].items
        self.assertEqual(items[0].title, 'Kaisa')

    def test_the_watching_link_points_at_the_one_person_page(self):
        self._grant(self.other, self.patient)

        item = build_dashboard(self.patient)['watching'].items[0]
        self.assertEqual(item.href, f'/care/person/{self.other.pk}/')


class NavigationTests(_Dash):
    """The dashboard must not become a second menu, but must be reachable."""

    #: Top-level destinations. Everything else lives under Dashboard.
    PRIMARY_NAV = ('/', '/assistant/', '/insights/', '/records/', '/dashboard/',
                   '/dashboard/settings/')

    #: Folded under Dashboard rather than removed.
    GROUPED = ('/dashboard/health/', '/care/', '/appointments/')

    def test_the_navigation_is_six_destinations(self):
        """
        ACCEPTANCE — the sidebar had twelve entries, four of which were
        different views of "caring". The complexity moved inside a group; it
        did not disappear, and neither did any capability.
        """
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()
        sidebar = body[body.index('class="sb-nav"'):body.index('</aside>')]

        for href in self.PRIMARY_NAV:
            self.assertIn(f'href="{href}"', sidebar,
                          f'{href} left the navigation')

        # The grouped ones are present, but never as top-level rows.
        for href in self.GROUPED:
            self.assertIn(f'href="{href}"', sidebar,
                          f'{href} is not reachable from the sidebar at all')
            # class= follows href= in the anchor, so look forward from it.
            at = sidebar.index(f'href="{href}"')
            self.assertIn('sb-sublink', sidebar[at:at + 120],
                          f'{href} is a top-level destination again')

    def test_dashboard_is_still_a_destination_not_only_a_toggle(self):
        """
        ACCEPTANCE — a parent that merely expands leaves no way to open the
        overview, which is the one screen answering "does anything need me".
        """
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()
        sidebar = body[body.index('class="sb-nav"'):body.index('</aside>')]

        self.assertIn('href="/dashboard/" class="sb-link sb-parent"', sidebar)
        self.assertIn('aria-expanded', sidebar)
        self.assertIn('aria-controls="dashSub"', sidebar)

    def test_the_group_is_collapsed_by_default(self):
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()

        self.assertIn('id="dashSub" hidden', body)

    def test_the_hidden_attribute_can_actually_hide_the_group(self):
        """
        ACCEPTANCE — it could not, and the panel was stuck open.

        `.sb-sub{display:flex}` is an author rule and beats the browser's
        `[hidden]{display:none}`, so toggling the attribute changed nothing on
        screen. The stylesheet needs a rule that out-specifies it.
        """
        from django.contrib.staticfiles import finders

        css = open(finders.find('css/main.css'), encoding='utf-8').read()
        self.assertIn('.sb-sub[hidden]', css,
                      'nothing in the stylesheet can hide the sub-group')

    def test_notifications_is_not_duplicated_in_the_sidebar(self):
        """
        The bell with its badge is in the top bar on every page and viewport,
        so a sidebar row would be the same destination twice on one screen.
        """
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()
        sidebar = body[body.index('class="sb-nav"'):body.index('</aside>')]

        self.assertNotIn('/notifications/', sidebar)
        # Still reachable — from the bell.
        self.assertIn('class="nav-notification"', body)

    def test_the_toggle_is_reachable_without_a_mouse(self):
        """A div with an onclick is invisible to keyboard and screen readers."""
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()
        sidebar = body[body.index('class="sb-nav"'):body.index('</aside>')]

        caret = sidebar[sidebar.index('id="dashCaret"') - 200:
                        sidebar.index('id="dashCaret"') + 260]
        self.assertIn('<button', caret)
        self.assertIn('aria-label', caret)

    def test_every_retired_destination_is_still_reachable(self):
        """
        Consolidation must not be deletion. Each capability that left the
        sidebar has to be one click from a hub — asserted by fetching the hub
        and finding the link, not by trusting that it was put there.
        """
        self.client.force_login(self.patient)

        hubs = {
            '/dashboard/health/': ['/records/', '/insights/'],
            '/care/':             ['/accounts/my-shares/'],
            '/dashboard/settings/': ['/accounts/my-shares/', '/accounts/consent/',
                                     '/accounts/emergency-card/',
                                     '/accounts/export/'],
        }
        for hub, expected in hubs.items():
            body = self.client.get(hub).content.decode()
            main = body[body.index('<main'):body.index('</main>')]
            for href in expected:
                self.assertIn(href, main, f'{href} is not reachable from {hub}')

    def test_appointments_is_reached_from_the_navigation_not_a_hub(self):
        """
        Appointments is not a retired destination — it has its own sidebar
        entry. It was also echoed on Companions, which made that page a second
        navigation menu instead of an answer to "who needs me?", so it was
        removed from there.

        The reachability guarantee still has to hold, so assert it where the
        link actually lives now: outside <main>, in the navigation.
        """
        self.client.force_login(self.patient)
        body = self.client.get('/care/').content.decode()
        main = body[body.index('<main'):body.index('</main>')]

        self.assertIn('/appointments/', body, 'Appointments left the navigation')
        self.assertNotIn('/appointments/', main,
                         'Appointments is echoed on Companions again')

    def test_the_caregiver_vocabulary_is_the_same_everywhere(self):
        """
        ACCEPTANCE — "Care", "Watching Over", "People I watch over" and
        "Sharing" were four names for one subject, and the dashboard and the
        sidebar disagreed about which to use.
        """
        self._grant(self.other, self.patient)
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()
        main = body[body.index('<main'):body.index('</main>')]

        self.assertIn('People I look after', main)
        self.assertNotIn('Watching Over', main)
        self.assertNotIn('Watching over', main)

    def test_each_dashboard_section_links_to_the_page_that_owns_it(self):
        data = build_dashboard(self.patient)

        self.assertEqual(data['sections']['medications'].href, '/care/')
        self.assertEqual(data['sections']['appointments'].href, '/appointments/')
        self.assertEqual(data['sections']['care'].href, '/care/')


class NoDuplicationTests(_Dash):
    """
    One fact, one place on the screen.

    A caregiver whose parent needed attention saw them twice: once in the
    attention digest and again in their own Watching Over row. The section is
    promoted above "Today" instead, so urgency is carried by position rather
    than by saying it twice.
    """

    def _person_needing_attention(self):
        self._grant(self.other, self.patient)
        MonitoringSignal.objects.create(
            patient=self.other,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=self.anchor, window_end=self.anchor,
            subject_key='task:z')

    def test_a_watched_person_is_not_also_in_the_attention_digest(self):
        """ACCEPTANCE — they appeared in both blocks on one screen."""
        self._person_needing_attention()

        data = build_dashboard(self.patient)
        self.assertEqual(data['attention'], [])
        self.assertTrue(data['watching_urgent'])

    def test_the_person_appears_exactly_once_on_the_page(self):
        self._person_needing_attention()
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()

        self.assertEqual(body.count('>Onni<'), 1)

    def test_the_page_order_is_today_then_attention_then_watching(self):
        """
        Fixed order, whoever needs what.

        An earlier version promoted Watching Over above Today when someone
        needed help, so the page rearranged itself between visits. A layout
        that moves is a layout people have to re-read; the attention block
        already carries urgency without the sections swapping places.
        """
        self._person_needing_attention()
        self._occurrence(TaskOccurrence.State.UNCONFIRMED, hours_ago=3)

        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()

        self.assertLess(body.index('>Today<'), body.index('Needs your attention'))
        self.assertLess(body.index('Needs your attention'),
                        body.index('People I look after'))

    def test_the_order_is_the_same_when_nobody_needs_help(self):
        self._grant(self.other, self.patient)
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()

        self.assertLess(body.index('>Today<'), body.index('People I look after'))

    def test_the_count_reads_as_english(self):
        self._person_needing_attention()

        item = build_dashboard(self.patient)['watching'].items[0]
        self.assertEqual(item.detail, '1 thing needs attention')


class SetupGuidanceTests(_Dash):
    """Reassurance must not be derived from having nothing to check."""

    def test_an_all_clear_is_qualified_when_nothing_is_set_up(self):
        """
        ACCEPTANCE — the page said "✅ Nothing needs your attention" to someone
        with no medication reminders and no records. That is a green tick for a
        question nobody asked.
        """
        Appointment.objects.create(
            patient=self.patient, title='Blood test',
            appointment_datetime=timezone.now() + timedelta(days=3))

        data = build_dashboard(self.patient)
        self.assertTrue(data['has_any_data'])
        self.assertFalse(data['fully_checked'])

        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'in what is set up so far')
        self.assertNotContains(response, 'Nothing needs your attention right now')

    def test_the_gap_says_what_would_fix_it(self):
        Appointment.objects.create(
            patient=self.patient, title='Blood test',
            appointment_datetime=timezone.now() + timedelta(days=3))

        gaps = build_dashboard(self.patient)['setup_gaps']
        self.assertIn('Set up medication reminders', [g['label'] for g in gaps])

    def test_a_full_all_clear_when_everything_reports(self):
        self._occurrence(TaskOccurrence.State.CONFIRMED, hours_ago=1)
        Appointment.objects.create(
            patient=self.patient, title='Blood test',
            appointment_datetime=timezone.now() + timedelta(days=3))

        data = build_dashboard(self.patient)
        self.assertTrue(data['fully_checked'])

        self.client.force_login(self.patient)
        self.assertContains(self.client.get('/dashboard/'),
                            'Nothing needs your attention right now')

    def test_no_reassurance_while_someone_watched_needs_help(self):
        """
        ACCEPTANCE — the banner was computed from this person's own health, so
        a caregiver read "nothing needs your attention" directly above a row
        saying their parent did.
        """
        Appointment.objects.create(
            patient=self.patient, title='Blood test',
            appointment_datetime=timezone.now() + timedelta(days=3))
        self._grant(self.other, self.patient)
        MonitoringSignal.objects.create(
            patient=self.other,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=self.anchor, window_end=self.anchor,
            subject_key='task:w')

        self.assertFalse(build_dashboard(self.patient)['show_reassurance'])

        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')
        self.assertNotContains(response, 'Nothing needs your attention')
        self.assertContains(response, '1 thing needs attention')

    def test_reassurance_returns_once_everyone_is_fine(self):
        self._occurrence(TaskOccurrence.State.CONFIRMED, hours_ago=1)
        Appointment.objects.create(
            patient=self.patient, title='Blood test',
            appointment_datetime=timezone.now() + timedelta(days=3))
        self._grant(self.other, self.patient)

        self.assertTrue(build_dashboard(self.patient)['show_reassurance'])

    def test_the_profile_menu_holds_only_account_things(self):
        """
        Dashboard is the first sidebar item, and Edit Profile / Change Password
        are on the Profile page this menu opens. Repeating them lengthened the
        list without making anything reachable.
        """
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/').content.decode()
        menu = body[body.index('id="userMenu"'):body.index('id="userMenu"') + 1800]

        for gone in ('/accounts/profile/edit/', '/accounts/password/change/'):
            self.assertNotIn(gone, menu, f'{gone} is back in the profile menu')
        self.assertNotIn('Dashboard</a>', menu)

        for kept in ('/accounts/profile/', '/accounts/consent/',
                     '/accounts/my-shares/', '/accounts/emergency-card/',
                     '/accounts/logout/'):
            self.assertIn(kept, menu, f'{kept} left the profile menu')

    def test_the_removed_entries_are_still_reachable(self):
        """Shortening a menu must not delete a feature."""
        self.client.force_login(self.patient)

        profile = self.client.get('/accounts/profile/').content.decode()
        self.assertIn('/accounts/profile/edit/', profile)
        self.assertIn('/accounts/password/change/', profile)

        settings_page = self.client.get('/dashboard/settings/').content.decode()
        self.assertIn('/accounts/password/change/', settings_page)

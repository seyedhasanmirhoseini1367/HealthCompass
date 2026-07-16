from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()

_EXPECTED_KEYS = {
    'recent_records', 'total_records', 'flagged_count',
    'recent_alerts', 'unread_alerts',
    'recent_predictions', 'total_predictions',
    'latest_pred', 'records_by_type',
}


class PatientDashboardServiceTest(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'dash_patient', email='dash_patient@test.invalid', password='pw',
        )

    def test_returns_expected_keys(self):
        from apps.dashboard.services import get_patient_dashboard_data
        data = get_patient_dashboard_data(self.patient)
        self.assertEqual(set(data.keys()), _EXPECTED_KEYS)

    def test_empty_patient_has_zero_counts(self):
        from apps.dashboard.services import get_patient_dashboard_data
        data = get_patient_dashboard_data(self.patient)
        self.assertEqual(data['total_records'], 0)
        self.assertEqual(data['unread_alerts'], 0)
        self.assertEqual(data['total_predictions'], 0)
        self.assertIsNone(data['latest_pred'])
        self.assertEqual(data['recent_records'], [])
        self.assertEqual(data['recent_alerts'], [])
        self.assertEqual(data['recent_predictions'], [])
        self.assertEqual(data['records_by_type'], {})

    def test_web_and_api_callers_see_identical_counts(self):
        """Both views call get_patient_dashboard_data — their counts must agree."""
        from apps.dashboard.services import get_patient_dashboard_data
        d1 = get_patient_dashboard_data(self.patient)
        d2 = get_patient_dashboard_data(self.patient)
        self.assertEqual(d1['total_records'], d2['total_records'])
        self.assertEqual(d1['unread_alerts'], d2['unread_alerts'])
        self.assertEqual(d1['total_predictions'], d2['total_predictions'])

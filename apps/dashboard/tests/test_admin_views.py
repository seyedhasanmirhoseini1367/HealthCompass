"""Monitoring and hospital-admin link management (prompt 9 task 2)."""
import io

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import DoctorProfile, HospitalAdminProfile, PatientDoctorRelationship
from apps.medical_records.parsers import WearableParser
from apps.testing.factories import make_doctor, make_patient, make_record_with_labs

User = get_user_model()


class MonitoringTests(TestCase):
    def test_allowed_roles_see_it_and_patients_do_not(self):
        make_record_with_labs(make_patient())
        for role, ok in (('data_scientist', True), ('hospital_admin', True), ('patient', False)):
            with self.subTest(role=role):
                user = User.objects.create_user(f'mon_{role}', email=f'm_{role}@test.invalid',
                                                password='pw', role=role)
                self.client.force_login(user)
                response = self.client.get('/dashboard/monitoring/')
                self.assertEqual(response.status_code == 200, ok)
                if not ok:
                    self.assertEqual(response.status_code, 302)

    def test_superuser_sees_it(self):
        self.client.force_login(User.objects.create_superuser('mon_su', email='su@test.invalid', password='pw'))
        self.assertEqual(self.client.get('/dashboard/monitoring/').status_code, 200)


class HospitalLinkTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('ha', email='ha@test.invalid', password='pw', role='hospital_admin')
        HospitalAdminProfile.objects.create(user=self.admin, hospital_name='Meilahti')
        self.patient = make_patient()
        self.doctor = make_doctor()
        DoctorProfile.objects.update_or_create(user=self.doctor, defaults={'hospital': 'Meilahti'})
        self.client.force_login(self.admin)

    def _create(self, doctor=None):
        return self.client.post('/dashboard/links/create/', {'patient_id': self.patient.pk,
                                                             'doctor_id': (doctor or self.doctor).pk})

    def test_creates_a_pending_link_for_a_doctor_in_the_same_hospital(self):
        self._create()
        link = PatientDoctorRelationship.objects.get()
        self.assertEqual(link.status, PatientDoctorRelationship.Status.PENDING)

    def test_refuses_a_doctor_from_another_hospital(self):
        outsider = make_doctor()
        DoctorProfile.objects.update_or_create(user=outsider, defaults={'hospital': 'Elsewhere'})
        self._create(outsider)
        self.assertFalse(PatientDoctorRelationship.objects.exists())

    def test_invalid_ids_are_refused(self):
        self.client.post('/dashboard/links/create/', {'patient_id': 999999, 'doctor_id': self.doctor.pk})
        self.assertFalse(PatientDoctorRelationship.objects.exists())

    def test_only_the_admin_who_linked_can_remove(self):
        self._create()
        link = PatientDoctorRelationship.objects.get()
        other = User.objects.create_user('ha2', email='ha2@test.invalid', password='pw', role='hospital_admin')
        self.client.force_login(other)
        self.assertEqual(self.client.post(f'/dashboard/links/{link.pk}/remove/').status_code, 404)
        self.client.force_login(self.admin)
        self.client.post(f'/dashboard/links/{link.pk}/remove/')
        self.assertFalse(PatientDoctorRelationship.objects.exists())

    def test_a_patient_cannot_create_links(self):
        self.client.force_login(self.patient)
        self._create()
        self.assertFalse(PatientDoctorRelationship.objects.exists())


APPLE = b"""<?xml version="1.0"?><HealthData>
<Record type="HKQuantityTypeIdentifierHeartRate" startDate="2026-01-01 08:00:00 +0200" value="61"/>
<Record type="HKQuantityTypeIdentifierStepCount" startDate="2026-01-01 09:00:00 +0200" value="1200"/>
<Record type="HKSomethingElse" startDate="2026-01-01 09:00:00 +0200" value="1"/>
<Record type="HKQuantityTypeIdentifierStepCount" startDate="bad date" value="3"/>
</HealthData>"""


class WearableFormatTests(SimpleTestCase):
    def parse(self, data, name):
        return WearableParser().parse(data, filename=name)

    def test_apple_health_xml(self):
        result = self.parse(APPLE, 'export.xml')
        self.assertEqual(result['device'], 'apple_health')
        self.assertEqual({p['metric'] for p in result['data_points']}, {'heart_rate', 'steps'})

    def test_oversized_apple_export_is_refused(self):
        parser = WearableParser()
        parser._XML_MAX_BYTES = 10
        self.assertTrue(parser.parse(APPLE, 'export.xml')['errors'])

    def test_vendor_detection_from_the_filename(self):
        for name, device in (('fitbit_export.csv', 'fitbit'), ('garmin.csv', 'garmin'), ('oura.csv', 'oura')):
            with self.subTest(name=name):
                result = self.parse(b'date,steps\n2026-01-01,10\n', name)
                self.assertEqual(result['device'], device)

    def test_json_object_and_list_shapes(self):
        for data in (b'[{"date": "2026-01-01", "steps": 10}]',
                     b'{"data": [{"date": "2026-01-01", "heart_rate": 60}]}',
                     b'{not json'):
            with self.subTest(data=data[:20]):
                self.assertIsInstance(self.parse(data, 'w.json'), dict)

    def test_excel_export(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl not installed')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['date', 'steps', 'heart_rate'])
        ws.append(['2026-01-01', 5000, 60])
        buf = io.BytesIO()
        wb.save(buf)
        result = self.parse(buf.getvalue(), 'w.xlsx')
        self.assertIsInstance(result, dict)

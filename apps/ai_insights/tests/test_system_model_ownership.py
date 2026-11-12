"""
REGRESSION — F5: an administrative flag decided data ownership, and deleting
that account destroyed patient prediction history.

The seizure integration picked its model's owner with

    User.objects.filter(is_staff=True).first() or request.user

so the owner was "whichever staff account has the lowest pk". Combined with
`AIModel.data_scientist` being CASCADE and `ModelPrediction.model` being
CASCADE, deleting that account cascaded:

    staff user -> AIModel -> ModelPrediction -> patient prediction history

reachable three ways that all exist today: the admin's reject-and-delete action,
an admin bulk delete, and that administrator's own GDPR erasure. Prediction
`input_file` uploads were orphaned in the same step.

This was not an attribution nit. It is patient-data loss triggered by ordinary
account administration, and it fired without anyone touching the model.

The fix has two halves:

  * platform-provisioned models have NO owner (`data_scientist = NULL`,
    `is_system = True`) — administrative authority never selects an owner;
  * `data_scientist` is SET_NULL, so erasing a real data scientist's account
    keeps their models and every prediction made with them.

PROTECT was rejected: it preserves the data by making GDPR erasure impossible,
and erasure is not optional. `ModelPrediction.model` deliberately stays CASCADE
— deleting a *model* is a separate, guarded administrative act, and the accident
being fixed here is the user chain.
"""
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase

from apps.ai_insights.models import AIModel, ModelPrediction

User = get_user_model()
SEIZURE_SLUG = 'eeg-seizure-detection'


class _Fixture(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'own_patient', email='own_patient@test.invalid', password='pw', role='patient')

    def _staff(self, username):
        return User.objects.create_user(
            username, email=f'{username}@test.invalid', password='pw', is_staff=True)

    def _system_model(self):
        return AIModel.objects.create(
            slug=SEIZURE_SLUG, name='EEG Seizure Detection', description='d',
            data_scientist=None, is_system=True)

    def _prediction(self, model, with_file=False):
        pred = ModelPrediction.objects.create(
            model=model, patient=self.patient, result={'label': 'Seizure'})
        if with_file:
            pred.input_file.save('eeg.parquet', ContentFile(b'PAR1'), save=True)
        return pred


class SystemOwnershipTests(_Fixture):
    """Administrative authority must never select an owner."""

    def test_a_system_model_has_no_owner(self):
        model = self._system_model()
        self.assertIsNone(model.data_scientist)
        self.assertTrue(model.is_system)

    def test_creating_a_staff_account_does_not_change_ownership(self):
        """ACCEPTANCE — F5. The owner used to be whoever had the lowest pk."""
        model = self._system_model()
        self._staff('own_staff_a')
        self._staff('own_staff_b')

        model.refresh_from_db()
        self.assertIsNone(model.data_scientist)

    def test_removing_a_staff_account_does_not_change_ownership(self):
        first = self._staff('own_staff_first')
        model = self._system_model()

        first.delete()

        model.refresh_from_db()
        self.assertIsNone(model.data_scientist)
        self.assertTrue(AIModel.objects.filter(slug=SEIZURE_SLUG).exists())

    def test_multiple_staff_accounts_do_not_affect_model_selection(self):
        for name in ('own_s1', 'own_s2', 'own_s3'):
            self._staff(name)
        model = self._system_model()

        found = AIModel.objects.get(slug=SEIZURE_SLUG)
        self.assertEqual(found.pk, model.pk)
        self.assertIsNone(found.data_scientist)

    def test_no_provisioning_path_queries_staff_for_an_owner(self):
        """
        Structural: the flag must not come back as an owner selector. A
        behavioural test cannot prove absence across both provisioning sites.
        """
        import pathlib
        import re

        for rel in ('apps/ai_insights/views/seizure.py', 'apps/api/views/predictions.py'):
            with self.subTest(path=rel):
                text = pathlib.Path(rel).read_text(encoding='utf-8')
                code = '\n'.join(re.sub(r'#.*$', '', line) for line in text.splitlines())
                self.assertNotIn('is_staff=True', code)
                self.assertNotIn('admin_user', code)


class AdminDeletionDoesNotDestroyHistoryTests(_Fixture):
    """The exact cascade discovered in the audit."""

    def test_deleting_the_first_staff_user_keeps_the_system_model(self):
        """ACCEPTANCE — F5. This deleted the model."""
        first = self._staff('own_first')
        model = self._system_model()
        self._prediction(model)

        first.delete()

        self.assertTrue(AIModel.objects.filter(slug=SEIZURE_SLUG).exists())

    def test_deleting_the_first_staff_user_keeps_patient_predictions(self):
        """ACCEPTANCE — F5. This destroyed the patient's seizure history."""
        first = self._staff('own_first2')
        model = self._system_model()
        prediction = self._prediction(model)

        first.delete()

        self.assertTrue(ModelPrediction.objects.filter(pk=prediction.pk).exists())
        self.assertEqual(
            ModelPrediction.objects.get(pk=prediction.pk).patient, self.patient)

    def test_deleting_another_staff_user_changes_nothing(self):
        self._staff('own_keep')
        other = self._staff('own_other')
        model = self._system_model()
        prediction = self._prediction(model)

        other.delete()

        self.assertTrue(AIModel.objects.filter(pk=model.pk).exists())
        self.assertTrue(ModelPrediction.objects.filter(pk=prediction.pk).exists())

    def test_a_prediction_input_file_is_not_orphaned_by_admin_deletion(self):
        first = self._staff('own_first3')
        model = self._system_model()
        prediction = self._prediction(model, with_file=True)
        storage, name = prediction.input_file.storage, prediction.input_file.name

        with self.captureOnCommitCallbacks(execute=True):
            first.delete()

        self.assertTrue(storage.exists(name),
                        'the file was removed along with a prediction that should have survived')


class DataScientistErasureTests(_Fixture):
    """A real submitter's account must still be erasable."""

    def _scientist(self):
        return User.objects.create_user(
            'own_ds', email='own_ds@test.invalid', password='pw', role='data_scientist')

    def test_erasing_a_data_scientist_keeps_their_model(self):
        scientist = self._scientist()
        model = AIModel.objects.create(
            data_scientist=scientist, name='Submitted', description='d')

        scientist.delete()

        model.refresh_from_db()
        self.assertIsNone(model.data_scientist)
        self.assertFalse(model.is_system)

    def test_erasing_a_data_scientist_keeps_patient_predictions(self):
        """The invariant: history is not collateral of account administration."""
        scientist = self._scientist()
        model = AIModel.objects.create(
            data_scientist=scientist, name='Submitted2', description='d')
        prediction = self._prediction(model)

        scientist.delete()

        self.assertTrue(ModelPrediction.objects.filter(pk=prediction.pk).exists())

    def test_gdpr_erasure_still_completes(self):
        from apps.accounts.services import purge_user_data

        scientist = self._scientist()
        model = AIModel.objects.create(
            data_scientist=scientist, name='Submitted3', description='d')
        model.model_file.save('m.onnx', ContentFile(b'fake'), save=True)
        storage, name = model.model_file.storage, model.model_file.name
        pk = scientist.pk

        with self.captureOnCommitCallbacks(execute=True):
            purge_user_data(scientist)

        self.assertFalse(User.objects.filter(pk=pk).exists())
        self.assertFalse(storage.exists(name),
                         'the submitter\'s own uploaded model file must still be erased')

    def test_an_ownerless_model_is_distinguishable_from_a_system_model(self):
        scientist = self._scientist()
        submitted = AIModel.objects.create(
            data_scientist=scientist, name='WasOwned', description='d')
        system = self._system_model()

        scientist.delete()
        submitted.refresh_from_db()

        self.assertIsNone(submitted.data_scientist)
        self.assertFalse(submitted.is_system)
        self.assertTrue(system.is_system)


class UserOwnedModelsUnchangedTests(_Fixture):
    """Existing behaviour for real submissions must be untouched."""

    def test_a_submitted_model_keeps_its_owner(self):
        scientist = User.objects.create_user(
            'own_ds2', email='own_ds2@test.invalid', password='pw', role='data_scientist')
        model = AIModel.objects.create(
            data_scientist=scientist, name='Mine', description='d')

        model.refresh_from_db()
        self.assertEqual(model.data_scientist, scientist)
        self.assertFalse(model.is_system)

    def test_my_models_still_lists_only_your_own(self):
        a = User.objects.create_user(
            'own_ds3', email='own_ds3@test.invalid', password='pw', role='data_scientist')
        b = User.objects.create_user(
            'own_ds4', email='own_ds4@test.invalid', password='pw', role='data_scientist')
        AIModel.objects.create(data_scientist=a, name='A model', description='d')
        AIModel.objects.create(data_scientist=b, name='B model', description='d')
        self._system_model()

        mine = AIModel.objects.filter(data_scientist=a)
        self.assertEqual([m.name for m in mine], ['A model'])

    def test_str_survives_an_absent_owner(self):
        """__str__ dereferenced the owner and would raise on NULL."""
        system = self._system_model()
        self.assertIn('system', str(system))

        scientist = User.objects.create_user(
            'own_ds5', email='own_ds5@test.invalid', password='pw', role='data_scientist')
        orphan = AIModel.objects.create(
            data_scientist=scientist, name='Orphan', description='d')
        scientist.delete()
        orphan.refresh_from_db()
        self.assertIn('no owner', str(orphan))


class SeizureFeatureStillWorksTests(_Fixture):
    """The provisioning path must still find and reuse the same model."""

    def test_get_or_create_returns_the_same_system_model(self):
        first, created_first = AIModel.objects.get_or_create(
            slug=SEIZURE_SLUG,
            defaults={'name': 'EEG Seizure Detection', 'description': 'd',
                      'data_scientist': None, 'is_system': True})
        second, created_second = AIModel.objects.get_or_create(
            slug=SEIZURE_SLUG,
            defaults={'name': 'EEG Seizure Detection', 'description': 'd',
                      'data_scientist': None, 'is_system': True})

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)

    def test_predictions_attach_to_the_system_model(self):
        model = self._system_model()
        prediction = self._prediction(model)
        self.assertEqual(prediction.model.slug, SEIZURE_SLUG)

    def test_the_model_is_still_provisioned_pending(self):
        self.assertEqual(self._system_model().status, AIModel.Status.PENDING)

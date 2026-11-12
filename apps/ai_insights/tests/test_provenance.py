"""
AI Models — provenance and input bounds.

Provenance
----------
`ModelPrediction` pointed at `AIModel` by foreign key and recorded nothing else.
A data scientist can replace `model_file` on an existing row, so every past
prediction silently appeared to come from the new artifact. A result a patient
was shown could not be traced back to the weights that produced it — which is
the first question anyone asks when a prediction turns out to be wrong.

`AIModel.version` and `AIModel.model_file_sha256` identify the artifact;
`ModelPrediction.model_version` and `.model_sha256` are copied from it at
creation and never rewritten.

`intended_use` is a governance field: a model with no stated population and no
stated contraindications cannot be reviewed for whether it fits the patient in
front of you.

Input bounds
------------
onnxruntime's `run()` cannot be interrupted, so a wall-clock timeout would
return control to the request while the computation carried on holding a core.
The bound is applied to the input instead, before the call.
"""
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

import pandas as pd

from apps.ai_insights.models import AIModel, ModelPrediction

User = get_user_model()


class ModelDigestTests(TestCase):

    def setUp(self):
        self.scientist = User.objects.create_user(
            'prov_ds', email='prov_ds@test.invalid', password='pw',
            role='data_scientist')

    def _model(self, name='Net', payload=b'fake-onnx-bytes'):
        model = AIModel.objects.create(
            data_scientist=self.scientist, name=name, description='d')
        model.model_file.save('net.onnx', ContentFile(payload), save=True)
        model.refresh_from_db()
        return model

    def test_uploading_a_file_records_its_digest(self):
        import hashlib

        payload = b'fake-onnx-bytes'
        model = self._model(payload=payload)
        self.assertEqual(model.model_file_sha256, hashlib.sha256(payload).hexdigest())

    def test_a_model_without_a_file_has_no_digest(self):
        model = AIModel.objects.create(
            data_scientist=self.scientist, name='No file', description='d')
        self.assertEqual(model.model_file_sha256, '')

    def test_replacing_the_file_changes_the_digest(self):
        model = self._model(payload=b'version-one')
        first = model.model_file_sha256

        model.model_file.save('net2.onnx', ContentFile(b'version-two'), save=True)
        model.refresh_from_db()
        self.assertNotEqual(model.model_file_sha256, first)

    def test_two_identical_files_have_the_same_digest(self):
        a = self._model(name='A', payload=b'same-bytes')
        b = self._model(name='B', payload=b'same-bytes')
        self.assertEqual(a.model_file_sha256, b.model_file_sha256)

    def test_a_model_starts_at_version_one(self):
        self.assertEqual(self._model().version, '1')


class PredictionProvenanceTests(TestCase):

    def setUp(self):
        self.scientist = User.objects.create_user(
            'prov_ds2', email='prov_ds2@test.invalid', password='pw',
            role='data_scientist')
        self.patient = User.objects.create_user(
            'prov_pat', email='prov_pat@test.invalid', password='pw', role='patient')

        self.model = AIModel.objects.create(
            data_scientist=self.scientist, name='Risk net', description='d',
            version='2.1')
        self.model.model_file.save('net.onnx', ContentFile(b'weights-v2'), save=True)
        self.model.refresh_from_db()

    def _predict(self):
        return ModelPrediction.objects.create(model=self.model, patient=self.patient)

    def test_a_prediction_records_the_version_it_used(self):
        self.assertEqual(self._predict().model_version, '2.1')

    def test_a_prediction_records_the_artifact_it_used(self):
        self.assertEqual(self._predict().model_sha256, self.model.model_file_sha256)

    def test_replacing_the_model_does_not_rewrite_past_predictions(self):
        """ACCEPTANCE — the FK alone let history be rewritten under our feet."""
        prediction = self._predict()
        original_sha = prediction.model_sha256

        self.model.version = '3.0'
        self.model.model_file.save('net3.onnx', ContentFile(b'weights-v3'), save=True)
        self.model.refresh_from_db()

        prediction.refresh_from_db()
        self.assertEqual(prediction.model_version, '2.1')
        self.assertEqual(prediction.model_sha256, original_sha)
        self.assertNotEqual(prediction.model_sha256, self.model.model_file_sha256)

    def test_saving_a_prediction_again_does_not_restamp_it(self):
        prediction = self._predict()
        original = (prediction.model_version, prediction.model_sha256)

        self.model.version = '9.9'
        self.model.save()

        prediction.notes = 'reviewed'
        prediction.save()
        prediction.refresh_from_db()
        self.assertEqual((prediction.model_version, prediction.model_sha256), original)

    def test_a_prediction_survives_a_model_without_a_file(self):
        model = AIModel.objects.create(
            data_scientist=self.scientist, name='Fileless', description='d')
        prediction = ModelPrediction.objects.create(model=model, patient=self.patient)
        self.assertEqual(prediction.model_sha256, '')


class IntendedUseTests(TestCase):

    def test_intended_use_is_stored(self):
        scientist = User.objects.create_user(
            'prov_ds3', email='prov_ds3@test.invalid', password='pw',
            role='data_scientist')
        text = ('Validated on adults 40-75 with known cardiovascular risk. '
                'NOT for use in pregnancy or under 18.')
        model = AIModel.objects.create(
            data_scientist=scientist, name='CV risk', description='d',
            intended_use=text)
        model.refresh_from_db()
        self.assertEqual(model.intended_use, text)

    def test_intended_use_is_optional_so_existing_models_still_load(self):
        scientist = User.objects.create_user(
            'prov_ds4', email='prov_ds4@test.invalid', password='pw',
            role='data_scientist')
        model = AIModel.objects.create(
            data_scientist=scientist, name='Legacy', description='d')
        self.assertEqual(model.intended_use, '')


class InputSizeBoundTests(TestCase):
    """A file within the upload limit can still expand past what is sane to run."""

    def _handler(self):
        from apps.ai_insights.inference.base import InferenceHandler

        scientist = User.objects.create_user(
            'prov_ds5', email='prov_ds5@test.invalid', password='pw',
            role='data_scientist')
        model = AIModel.objects.create(
            data_scientist=scientist, name='Sized', description='d')
        return InferenceHandler(model)

    @override_settings(MAX_INFERENCE_INPUT_CELLS=100)
    def test_an_oversized_input_is_refused_before_inference(self):
        from apps.ai_insights.inference.base import InferenceError

        frame = pd.DataFrame([[0.0] * 20] * 20)   # 400 cells
        with self.assertRaises(InferenceError) as caught:
            self._handler()._check_input_size(frame)

        message = str(caught.exception)
        self.assertIn('too large', message)
        self.assertIn('20', message)

    @override_settings(MAX_INFERENCE_INPUT_CELLS=1000)
    def test_a_normal_input_passes(self):
        frame = pd.DataFrame([[0.0] * 10] * 10)   # 100 cells
        self._handler()._check_input_size(frame)   # must not raise

    @override_settings(MAX_INFERENCE_INPUT_CELLS=100)
    def test_the_refusal_tells_the_patient_what_to_do(self):
        from apps.ai_insights.inference.base import InferenceError

        frame = pd.DataFrame([[0.0] * 20] * 20)
        with self.assertRaises(InferenceError) as caught:
            self._handler()._check_input_size(frame)
        self.assertIn('smaller', str(caught.exception).lower())

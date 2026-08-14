"""
REGRESSION — the seizure ONNX models could never be loaded.

`_MODELS_DIR` was `Path(__file__).parent / "model_weights"`. This module lives
in `apps/ai_insights/inference/`, so that resolved to
`apps/ai_insights/inference/model_weights` — a directory that has never existed.
The files are one level up, in `apps/ai_insights/model_weights`.

Every variant therefore raised FileNotFoundError inside `_get_session`. That did
not surface as a missing path, because `predict()` catches per-model exceptions
and only raises when all three fail, so the operator saw
"All models failed: ONNX model not found: …" — a message about the models, not
about the path. The featured EEG tool on the catalog page was dead.

These tests pin both halves: the files are where the code looks, and the whole
pipeline produces a result from the bundled sample.

THESE TESTS DO NOT VALIDATE THE MODELS.
----------------------------------------
The true label of `static/samples/eeg_sample.parquet` is **unknown**. The file
ships with no provenance — no dataset citation, no README, no label column, and
its only other reference in the codebase is as a demo loader in
`seizure_realtime.html`. Nothing here establishes whether the models are right,
only that they run and keep giving the same answer.

So no test asserts a specific label as correct. The end-to-end tests assert the
pipeline completes, that all three variants succeed, and that repeated runs
agree — a regression guard against the path breaking again or preprocessing
drifting. A pinned prediction with no ground truth behind it would read as
validation while measuring nothing, and on a clinical model that misreading is
the dangerous kind.

Real validation needs a labelled holdout set with known provenance, and that
does not exist in this repository.
"""
from pathlib import Path

from django.test import SimpleTestCase

from apps.ai_insights.inference.seizure_inference import (
    ONNX_FILES, _MODELS_DIR, predict,
)

SAMPLE = Path('static/samples/eeg_sample.parquet')


class ModelFileTests(SimpleTestCase):

    def test_the_weights_directory_exists(self):
        """ACCEPTANCE — this pointed at a directory that never existed."""
        self.assertTrue(_MODELS_DIR.is_dir(), f'not a directory: {_MODELS_DIR}')

    def test_every_declared_onnx_file_is_present(self):
        for variant, filename in ONNX_FILES.items():
            with self.subTest(variant=variant):
                path = _MODELS_DIR / filename
                self.assertTrue(path.exists(), f'missing: {path}')
                self.assertGreater(path.stat().st_size, 1024,
                                   f'{path} is too small to be a model')

    def test_the_directory_is_not_under_inference(self):
        """
        Pins the specific mistake. `inference/model_weights` is the path the
        code used to build, and it is empty of meaning — nothing has ever been
        shipped there.
        """
        self.assertNotIn('inference', _MODELS_DIR.parts[-2:])
        self.assertEqual(_MODELS_DIR.name, 'model_weights')

    def test_no_stray_pth_files_are_expected(self):
        """
        The .pth weights were removed once ONNX became the only thing loaded.
        Nothing here may start depending on them again.
        """
        self.assertEqual(list(_MODELS_DIR.glob('*.pth')), [])
        for filename in ONNX_FILES.values():
            self.assertTrue(filename.endswith('.onnx'), filename)

    def test_each_variant_loads_as_a_session(self):
        from apps.ai_insights.inference.seizure_inference import _get_session

        for variant in ONNX_FILES:
            with self.subTest(variant=variant):
                session = _get_session(variant)
                self.assertTrue(session.get_inputs(), f'{variant} has no inputs')


class EndToEndTests(SimpleTestCase):
    """The bundled sample through the real pipeline — no mocks."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not SAMPLE.exists():
            raise cls.skipException(f'sample not present: {SAMPLE}')
        import pandas as pd
        cls.frame = pd.read_parquet(SAMPLE)

    def _data(self):
        return self.frame.to_dict('list')

    def test_the_sample_has_the_expected_shape(self):
        """19 EEG channels plus EKG, which predict() drops."""
        self.assertEqual(len(self.frame.columns), 20)
        self.assertIn('EKG', self.frame.columns)
        self.assertGreater(len(self.frame), 2000)

    def test_the_ensemble_returns_a_label(self):
        """
        ACCEPTANCE — this raised 'All models failed' for every input.

        Asserts the label is one of the two the model can emit, NOT that it is
        the clinically correct one: the sample's true label is unknown (see the
        module docstring).
        """
        result = predict(self._data(), variant='ensemble')

        self.assertIn(result['label'], ('Seizure', 'LPD (Lateralised Periodic Discharge)'))
        self.assertEqual(result['variant'], 'ensemble')

    def test_all_three_models_succeed(self):
        result = predict(self._data(), variant='ensemble')
        failed = [m for m in result['per_model'] if not m.get('success')]
        self.assertEqual(failed, [], f'variants failed: {failed}')

    def test_the_confidence_is_a_probability(self):
        result = predict(self._data(), variant='ensemble')
        self.assertGreaterEqual(result['confidence'], 0.0)
        self.assertLessEqual(result['confidence'], 1.0)

    def test_the_votes_account_for_every_successful_model(self):
        result = predict(self._data(), variant='ensemble')
        succeeded = sum(1 for m in result['per_model'] if m.get('success'))
        self.assertEqual(sum(result['votes'].values()), succeeded)

    def test_each_variant_runs_on_its_own(self):
        for variant in ONNX_FILES:
            with self.subTest(variant=variant):
                result = predict(self._data(), variant=variant)
                self.assertIn('label', result)
                self.assertIn('confidence', result)

    def test_the_result_is_deterministic(self):
        """
        Same input, same weights, same answer. A drift here means preprocessing
        or the session options changed, which is exactly what the path fix was
        not allowed to do.

        Self-consistency, not correctness: it compares two runs against each
        other, never against a known-correct label.
        """
        first = predict(self._data(), variant='cnn_transformer')
        second = predict(self._data(), variant='cnn_transformer')
        self.assertEqual(first['label'], second['label'])
        self.assertAlmostEqual(first['confidence'], second['confidence'], places=6)

    def test_the_ekg_channel_is_dropped_not_fed_to_the_model(self):
        """19 channels is the trained input; EKG is not one of them."""
        without = {k: v for k, v in self._data().items() if k != 'EKG'}
        with_ekg = predict(self._data(), variant='cnn_transformer')
        no_ekg = predict(without, variant='cnn_transformer')

        self.assertEqual(with_ekg['label'], no_ekg['label'])
        self.assertAlmostEqual(with_ekg['confidence'], no_ekg['confidence'], places=6)


class MissingModelTests(SimpleTestCase):
    """A missing file must be loud, not a quietly wrong answer."""

    def test_an_absent_model_raises_rather_than_guessing(self):
        from unittest.mock import patch

        from apps.ai_insights.inference import seizure_inference as si

        with patch.dict(si._SESSION_CACHE, {}, clear=True):
            with patch.object(si, '_MODELS_DIR', Path('/nonexistent/model_weights')):
                with self.assertRaises(FileNotFoundError):
                    si._get_session('cnn_transformer')

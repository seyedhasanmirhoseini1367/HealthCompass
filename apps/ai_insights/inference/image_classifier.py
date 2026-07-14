# ai_insights/inference/image_classifier.py
"""
Image classification handler — supports JPG, PNG, BMP, TIFF.
Requires an ONNX model file (.onnx).  Keras (.h5/.keras) and PyTorch (.pt/.pth)
are blocked by _load_model() for security.

handler_slug: "image_classifier"

handler_config:
{
    "target_size": [224, 224],          # resize target (width, height) — required
    "normalize": true,                  # divide pixel values by 255.0 (default: true)
    "channels": 3,                      # 1 = grayscale, 3 = RGB (default: 3)
    "label_map": {"0": "Normal", "1": "Abnormal"},
    "expected_n_features": null         # leave null for image models
}
"""

import numpy as np
from .registry import register
from .base import InferenceHandler, InferenceError


@register("image_classifier")
class ImageClassifierHandler(InferenceHandler):

    accepted_extensions = ['jpg', 'jpeg', 'png', 'bmp', 'tiff', 'tif']

    def load_and_preprocess(self, file, filename: str):
        target_size = self.cfg.get('target_size', [224, 224])
        channels    = self.cfg.get('channels', 3)
        normalize   = self.cfg.get('normalize', True)

        img_array = self.read_image(file, target_size=tuple(target_size))

        if channels == 1:
            try:
                from PIL import Image
                import io
                file.seek(0)
                img = Image.open(io.BytesIO(file.read())).convert('L')
                img = img.resize(tuple(target_size))
                img_array = np.array(img)[..., np.newaxis]
            except Exception as e:
                raise InferenceError(f'Could not convert image to grayscale: {e}')

        if normalize:
            img_array = img_array.astype('float32') / 255.0

        # Shape: (1, H, W, C)
        feature_df = None  # not used directly
        summary = {
            'format':   filename.rsplit('.', 1)[-1].upper(),
            'shape':    list(img_array.shape),
            'channels': channels,
        }
        # Store array on self for use in run()
        self._img_array = img_array
        return None, summary

    def run(self, uploaded_file=None, input_data: dict | None = None) -> dict:
        if uploaded_file is None:
            raise InferenceError('This model requires an image file upload.')

        filename = getattr(uploaded_file, 'name', '').lower()
        self.validate_file(uploaded_file, filename)
        _, input_summary = self.load_and_preprocess(uploaded_file, filename)

        sess = self._load_model()  # InferenceSession (ONNX-only)

        img = self._img_array
        if img.ndim == 3:
            img = np.expand_dims(img, axis=0)  # → (1, H, W, C)

        inp       = sess.get_inputs()[0]
        inp_shape = inp.shape
        # Detect NCHW layout: shape[1] is an integer channel count of 1 or 3
        if (len(inp_shape) == 4 and
                isinstance(inp_shape[1], int) and
                inp_shape[1] in (1, 3)):
            img = np.transpose(img, (0, 3, 1, 2))  # NHWC → NCHW

        try:
            outputs = sess.run(None, {inp.name: img.astype(np.float32)})
        except Exception as e:
            raise InferenceError(f'Model prediction failed: {e}')

        raw = outputs[0][0]   # (num_classes,) or scalar
        if raw.ndim > 0 and raw.shape[-1] > 1:
            pred_class = int(np.argmax(raw))
            proba      = float(np.max(raw))
        else:
            proba      = float(raw[0] if raw.ndim > 0 else raw)
            pred_class = 1 if proba >= 0.5 else 0

        label_map = self.cfg.get('label_map', {})
        label     = label_map.get(str(pred_class), str(pred_class))

        return {
            'success':           True,
            'prediction':        pred_class,
            'prediction_label':  label,
            'prediction_proba':  proba,
            'risk_score':        proba,
            'label':             label,
            'input_summary':     input_summary,
            'input_data':        {'image_shape': str(list(self._img_array.shape))},
        }

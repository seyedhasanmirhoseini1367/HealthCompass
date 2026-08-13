import json
from django import forms
from .models import AIModel


class SubmitModelForm(forms.ModelForm):
    input_schema_text = forms.CharField(
        label='Input schema (JSON)',
        widget=forms.Textarea(attrs={'rows': 5, 'placeholder': '{"age": "integer", "glucose": "float", ...}'}),
        help_text='Describe the input fields your model expects as a JSON object. Leave as {} for file-only models.',
        required=False,
        initial='{}',
    )
    output_schema_text = forms.CharField(
        label='Output schema (JSON)',
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': '{"risk_score": "float 0-1", "label": "string"}'}),
        help_text='Describe what your model outputs.',
        required=False,
        initial='{}',
    )

    handler_config_text = forms.CharField(
        label='Handler Config (JSON)',
        widget=forms.Textarea(attrs={
            'rows': 4,
            'placeholder': '{"sampling_rate_hz": 256, "n_channels": 19, "label_map": {"0": "Normal", "1": "Seizure"}}',
        }),
        required=False,
        help_text='Advanced: JSON config for your handler (see ADMIN_CONFIGS.md). Leave blank if not using a custom handler.',
    )

    class Meta:
        model = AIModel
        fields = ['name', 'category', 'input_type', 'description', 'interpretation_guide',
                  'handler_slug', 'model_file']
        widgets = {
            'description':          forms.Textarea(attrs={'rows': 4}),
            'interpretation_guide': forms.Textarea(attrs={
                'rows': 5,
                'placeholder': (
                    'Describe the disease/condition, what the risk levels mean, '
                    'what patients should do at high/moderate/low risk, '
                    'and any important medical context. Gemini will use this to '
                    'generate a personalised explanation after each prediction.'
                ),
            }),
        }

    #: Only ONNX is accepted. base.py hard-blocks pickle/Keras/joblib/PyTorch at
    #: LOAD time because deserialising them executes arbitrary code — but that is
    #: after the file is already stored, so a blocked format sat in the bucket
    #: until someone tried to run it. Rejecting at upload keeps it out entirely.
    ALLOWED_MODEL_EXTS = ['onnx']

    def clean_model_file(self):
        """
        Validate the uploaded model by extension, size and magic bytes.

        There was no validation at all here — unlike the medical-record uploader,
        which checks all three. A data scientist could upload any file of any
        size under the name of a model.
        """
        from django.conf import settings
        from apps.medical_records.services import validate_upload

        f = self.cleaned_data.get('model_file')
        if not f:
            return f

        ok, payload = validate_upload(f, self.ALLOWED_MODEL_EXTS)
        if not ok:
            raise forms.ValidationError(payload)

        limit = int(getattr(settings, 'MAX_MODEL_UPLOAD_BYTES', 200 * 1024 * 1024))
        size = getattr(f, 'size', None)
        if size is not None and size > limit:
            raise forms.ValidationError(
                f'Model file is too large ({size // (1024 * 1024)} MB). '
                f'Maximum accepted size is {limit // (1024 * 1024)} MB.')

        # ONNX is a protobuf; there is no single reliable magic prefix, so
        # confirm onnxruntime can actually parse it rather than guessing from
        # bytes. This is the check that keeps a renamed pickle out of storage.
        try:
            import onnxruntime as ort
            data = f.read()
            f.seek(0)
            ort.InferenceSession(data, providers=['CPUExecutionProvider'])
        except ImportError:
            pass          # runtime absent in this environment; load-time check still applies
        except Exception:
            raise forms.ValidationError(
                'This file could not be parsed as an ONNX model. Convert your '
                'model to ONNX (see convert_to_onnx.py) and upload the .onnx file.')
        return f

    def clean_input_schema_text(self):
        val = (self.cleaned_data.get('input_schema_text') or '{}').strip()
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            raise forms.ValidationError('Invalid JSON. Please check your schema.')

    def clean_output_schema_text(self):
        val = (self.cleaned_data.get('output_schema_text') or '{}').strip()
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            raise forms.ValidationError('Invalid JSON. Please check your schema.')

    def clean_handler_config_text(self):
        val = self.cleaned_data.get('handler_config_text', '').strip()
        if not val:
            return {}
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            raise forms.ValidationError('Invalid JSON in handler config.')

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.input_schema   = self.cleaned_data['input_schema_text']
        instance.output_schema  = self.cleaned_data['output_schema_text']
        instance.handler_config = self.cleaned_data.get('handler_config_text') or {}
        if commit:
            instance.save()
        return instance

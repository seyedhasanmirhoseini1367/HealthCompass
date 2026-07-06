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

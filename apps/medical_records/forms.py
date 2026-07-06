from django import forms
from .models import MedicalRecord


class KantaUploadForm(forms.Form):
    xml_file = forms.FileField(
        label='Kanta XML file',
        help_text='Upload the XML file exported from Kanta (OmaKanta).',
        widget=forms.ClearableFileInput(attrs={'accept': '.xml'}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Optional notes…'}),
    )


class WearableUploadForm(forms.Form):
    DEVICE_CHOICES = [
        ('auto', 'Auto-detect'),
        ('fitbit', 'Fitbit'),
        ('garmin', 'Garmin'),
        ('oura', 'Oura Ring'),
        ('apple_health', 'Apple Health'),
        ('other', 'Other'),
    ]
    csv_file = forms.FileField(
        label='CSV file',
        help_text='Export data as CSV from your device app and upload here.',
        widget=forms.ClearableFileInput(attrs={'accept': '.csv'}),
    )
    device = forms.ChoiceField(choices=DEVICE_CHOICES, initial='auto')
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Optional notes…'}),
    )


class TextPasteForm(forms.Form):
    RECORD_TYPE_CHOICES = [('auto', 'Auto-detect (AI)')] + list(MedicalRecord.RecordType.choices)

    text = forms.CharField(
        label='Medical text',
        widget=forms.Textarea(attrs={
            'rows': 12,
            'placeholder': (
                'Paste any medical text here — lab results, prescriptions, '
                'doctor notes, discharge summaries…\n\n'
                'Example:\n'
                'Glucose: 5.4 mmol/L (ref 3.9–5.6)\n'
                'HbA1c: 38 mmol/mol (ref <48)\n'
                'Cholesterol: 4.8 mmol/L'
            ),
        }),
        help_text='AI will extract lab values, medications, diagnoses and dates automatically.',
    )
    record_type = forms.ChoiceField(
        choices=RECORD_TYPE_CHOICES,
        initial='auto',
        label='Record type',
        help_text='Leave on Auto-detect to let AI decide.',
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Optional personal notes…'}),
    )


class PDFUploadForm(forms.Form):
    pdf_file = forms.FileField(
        label='PDF file',
        help_text='Upload a medical document PDF. AI will extract and structure the content.',
        widget=forms.ClearableFileInput(attrs={'accept': '.pdf'}),
    )
    record_type = forms.ChoiceField(
        choices=MedicalRecord.RecordType.choices,
        initial=MedicalRecord.RecordType.OTHER,
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Optional notes…'}),
    )

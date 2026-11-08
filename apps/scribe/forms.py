import os

from django import forms

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = ('.mp3', '.wav', '.m4a')


def _looks_like_audio(head: bytes, ext: str) -> bool:
    """
    Cheap magic-byte check so an arbitrary file renamed to .mp3 is refused
    before it reaches the decoder.
    """
    if ext == '.wav':
        return head[:4] == b'RIFF' and head[8:12] == b'WAVE'
    if ext == '.m4a':
        return head[4:8] == b'ftyp'
    if ext == '.mp3':
        # ID3 tag, or a bare MPEG audio frame sync (11 set bits).
        return head[:3] == b'ID3' or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    return False


class EncounterUploadForm(forms.Form):
    audio = forms.FileField(
        label='Consultation recording',
        help_text='mp3, wav or m4a — up to 25 MB.',
        widget=forms.ClearableFileInput(attrs={'accept': '.mp3,.wav,.m4a,audio/*'}),
    )

    def clean_audio(self):
        f = self.cleaned_data['audio']
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise forms.ValidationError('Only mp3, wav and m4a files are supported.')
        if f.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError('The file is larger than 25 MB.')
        head = f.read(16)
        f.seek(0)
        if not _looks_like_audio(head, ext):
            raise forms.ValidationError(f'The file does not look like a valid {ext} audio file.')
        return f

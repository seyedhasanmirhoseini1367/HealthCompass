"""Field validators shared by the web forms and the API serializers (audit CQ-02),
so a value one surface refuses is refused by the other too."""
import re

from django.core.exceptions import ValidationError

PHONE_RE = re.compile(r'^[\d\s\+\-\(\)\.]{0,20}$')
PHONE_MESSAGE = 'Enter a valid phone number (digits, spaces, +, -, ( ) allowed).'


def validate_phone(value):
    if value and not PHONE_RE.match(value):
        raise ValidationError(PHONE_MESSAGE)

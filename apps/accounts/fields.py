"""
EncryptedCharField — AES-128-CBC + HMAC-SHA256 (Fernet) at the model layer.

Key derivation: PBKDF2-HMAC-SHA256 over settings.SECRET_KEY, 100 000 iterations,
32-byte output → base64url-encoded Fernet key.  The key is cached per-process so
the derivation runs only once.

Trade-offs accepted:
- Key is tied to SECRET_KEY. Rotating the key requires a data migration that
  re-encrypts all rows.  Document in the ops runbook before go-live.
- Not searchable: you cannot do .filter(national_id=x) against encrypted rows.
  Use the raw value in Python after fetching the object.
- Legacy plaintext rows (written before this field was added) are returned as-is
  and will fail HMAC verification. The fallback returns the raw stored value.
"""
import base64
import functools
import hashlib

from django.conf import settings
from django.db import models


@functools.lru_cache(maxsize=1)
def _fernet():
    """Return a cached Fernet instance derived from SECRET_KEY."""
    from cryptography.fernet import Fernet
    dk = hashlib.pbkdf2_hmac(
        'sha256',
        settings.SECRET_KEY.encode(),
        b'healthcompass-field-key',  # static salt — key rotation via SECRET_KEY change
        100_000,
        dklen=32,
    )
    return Fernet(base64.urlsafe_b64encode(dk))


class EncryptedCharField(models.TextField):
    """
    Stores a string value encrypted (Fernet) in a TextField column.

    max_length on the parent CharField is advisory only; the DB column is TEXT.
    The ciphertext is base64 (~130 chars for a 15-char plaintext).
    """

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except Exception:
            # Legacy plaintext or wrong key — return as-is, log for ops.
            import logging
            logging.getLogger(__name__).warning(
                'EncryptedCharField: decryption failed; returning raw value. '
                'This is expected for legacy plaintext rows before encryption was enabled.'
            )
            return value

    def get_prep_value(self, value):
        if not value:
            return value
        return _fernet().encrypt(value.encode()).decode()

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        # Use the full dotted path so migrations can reconstruct the field
        path = 'apps.accounts.fields.EncryptedCharField'
        return name, path, args, kwargs

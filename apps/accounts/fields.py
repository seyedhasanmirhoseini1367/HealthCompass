"""
EncryptedCharField — Fernet (AES-128-CBC + HMAC-SHA256) at the model layer.

Keys (audit SEC-15)
-------------------
settings.FIELD_ENCRYPTION_KEYS is a list of Fernet keys, newest first. New
values are encrypted with the first; every key is tried on read
(cryptography's MultiFernet). The key the project used before this setting
existed, derived from SECRET_KEY with PBKDF2, is always appended LAST, so data
written under it stays readable until `rotate_field_encryption` re-encrypts it.
With no keys configured the derived key is the only key, as before.

Fail closed
-----------
A value no key can decrypt is never handed to the caller as if it were the
plaintext (the old fallback returned the raw ciphertext). It loads as an
`UnreadableValue`: an empty string flagged `.unreadable`, which templates show
as nothing and views can show as "unavailable". It keeps the stored bytes, so
saving the row writes them back unchanged rather than wiping the field. Code
that must have the real value calls `require_readable()`, which raises
FieldDecryptionError.

Not searchable: you cannot .filter(national_id=x) against encrypted rows.
"""
import base64
import functools
import hashlib
import logging

from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)


class FieldDecryptionError(Exception):
    """An encrypted field could not be decrypted with any configured key."""


class UnreadableValue(str):
    """Placeholder for a value that could not be decrypted. Empty as a string;
    carries the stored ciphertext so it is preserved on save."""
    unreadable = True

    def __new__(cls, raw):
        obj = super().__new__(cls, '')
        obj.raw = raw
        return obj

    def __repr__(self):
        return '<UnreadableValue>'


def require_readable(value):
    if isinstance(value, UnreadableValue):
        raise FieldDecryptionError('This value could not be decrypted with the configured keys.')
    return value


def _legacy_key() -> bytes:
    dk = hashlib.pbkdf2_hmac('sha256', settings.SECRET_KEY.encode(),
                             b'healthcompass-field-key', 100_000, dklen=32)
    return base64.urlsafe_b64encode(dk)


def _configured_keys() -> tuple:
    keys = getattr(settings, 'FIELD_ENCRYPTION_KEYS', None) or []
    if isinstance(keys, str):
        keys = [k for k in keys.split(',')]
    return tuple(k.strip() for k in keys if k and k.strip())


@functools.lru_cache(maxsize=4)
def _multifernet(keys: tuple, secret_key: str):
    from cryptography.fernet import Fernet, MultiFernet
    fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in keys]
    fernets.append(Fernet(_legacy_key()))
    return MultiFernet(fernets)


def fernet():
    """The MultiFernet for the current settings (primary key first)."""
    return _multifernet(_configured_keys(), settings.SECRET_KEY)


def decrypt(token: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as exc:
        raise FieldDecryptionError(str(exc) or 'invalid token') from exc


def encrypt(plaintext: str) -> str:
    return fernet().encrypt(plaintext.encode()).decode()


class EncryptedCharField(models.TextField):
    """
    Stores a string value encrypted (Fernet) in a TextField column.

    max_length is advisory only; the DB column is TEXT. The ciphertext is
    base64 (~130 chars for a 15-character plaintext).
    """

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return decrypt(value)
        except FieldDecryptionError:
            logger.warning('EncryptedCharField: a stored value could not be decrypted '
                           'with any configured key; returning it as unreadable.')
            return UnreadableValue(value)

    def get_prep_value(self, value):
        if isinstance(value, UnreadableValue):
            return value.raw  # unchanged: never overwrite what we could not read
        if not value:
            return value
        return encrypt(str(value))

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        path = 'apps.accounts.fields.EncryptedCharField'
        return name, path, args, kwargs

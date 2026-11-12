# Field encryption keys

`EncryptedCharField` (today: `PatientProfile.national_id`) is encrypted with
Fernet. Keys come from `FIELD_ENCRYPTION_KEYS`, a comma-separated list, newest
first. New values use the first key; reads try every key, and the key derived
from `SECRET_KEY` (what the project used before this setting) is always tried
last.

## First deployment

1. Generate a key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Set `FIELD_ENCRYPTION_KEYS=<that key>` and deploy. Existing rows still read
   through the derived key.
3. `python manage.py rotate_field_encryption` (dry run), then
   `python manage.py rotate_field_encryption --apply --include-plaintext`.
   After this no row depends on `SECRET_KEY`, so `SECRET_KEY` can be rotated
   without losing data.

## Rotating later

1. Generate a new key and put it FIRST: `FIELD_ENCRYPTION_KEYS=<new>,<old>`.
2. Deploy, then `python manage.py rotate_field_encryption --apply`.
3. When the dry run reports 0 values needing the old key, remove `<old>`.

## When a value cannot be decrypted

It is never shown as if it were the value. It loads as an empty, flagged
`UnreadableValue`; the profile shows "unavailable", and saving the profile
writes the stored bytes back unchanged. The rotation command counts these
under "unreadable with every key": restore the missing key to recover them.

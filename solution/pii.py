"""PII protection: one secret key -> (a) keyed phone hash for matching, (b) encryption for text.

- Phone: HMAC-SHA256(key, canonical phone). Deterministic, so events can still be matched to
  leads by equality, but not reversible without the key. A *plain* SHA-256 would not do: there
  are only ~4 billion Indian mobile numbers, so anyone could precompute them all.
- Other PII text (customer name): Fernet (AES-128-CBC + HMAC), reversible with the key.
- The full phone number is never stored; only its hash and last 4 digits (for masking).
- One master key, two derived sub-keys, so a leak of one use does not hand over the other.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

KEY_ENV = "AU_PII_KEY"
KEY_FILE = Path(__file__).with_name(".pii_key")  # git-ignored by solution/.gitignore


def load_master_key(path: Path = KEY_FILE) -> bytes:
    """Env var wins. Otherwise read the key file, creating it on first use.

    Fine for a take-home that must run from a clean clone. In production the key would come
    from a secrets manager / KMS, never sit next to the database.
    """
    env = os.environ.get(KEY_ENV)
    if env:
        return env.encode()
    if not path.exists():
        try:
            with open(path, "x", encoding="ascii") as fh:  # "x": never overwrite an existing key
                fh.write(secrets.token_hex(32))
            os.chmod(path, 0o600)  # best effort; no-op on Windows
        except FileExistsError:  # another process created it first
            pass
    return path.read_text(encoding="ascii").strip().encode()


def _derive(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, label, hashlib.sha256).digest()


class Pii:
    def __init__(self, master: bytes):
        self._mac_key = _derive(master, b"au/phone-hmac/v1")
        self._fernet = Fernet(base64.urlsafe_b64encode(_derive(master, b"au/fernet/v1")))
        self._check = _derive(master, b"au/key-check/v1").hex()

    def phone_hash(self, canonical_phone: str) -> bytes:
        return hmac.new(self._mac_key, canonical_phone.encode("ascii"), hashlib.sha256).digest()

    @staticmethod
    def last4(canonical_phone: str) -> str:
        return canonical_phone[-4:]

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")

    @property
    def key_fingerprint(self) -> str:
        """Safe-to-store proof of which key was used; lets the DB detect a wrong/lost key."""
        return self._check

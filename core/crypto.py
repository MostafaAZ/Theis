"""Encryption at rest for buyer documents and identity numbers (FR-68, NFR-25)."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        # Derive a stable key from SECRET_KEY for development.
        digest = hashlib.sha256(("kubbar-files:" + settings.SECRET_KEY).encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    return _fernet().decrypt(token)


def encrypt_text(value: str) -> str:
    if not value:
        return ""
    return encrypt_bytes(value.encode()).decode()


def decrypt_text(value: str) -> str:
    if not value:
        return ""
    try:
        return decrypt_bytes(value.encode()).decode()
    except InvalidToken:
        return ""

"""Cryptographic utilities for secure field encryption at rest."""
import base64
import hashlib
import logging
from typing import Optional
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger('apps.common.encryption')

_fernet_instance: Optional[Fernet] = None


def get_fernet() -> Fernet:
    """Returns a cached Fernet cipher instance using FIELD_ENCRYPTION_KEY."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    raw_key = getattr(settings, 'FIELD_ENCRYPTION_KEY', None)

    if not raw_key:
        secret = getattr(settings, 'SECRET_KEY', None)
        if not secret:
            raise ImproperlyConfigured("SECRET_KEY must be configured to initialize field encryption.")
        derived = base64.urlsafe_b64encode(hashlib.sha256(f"field-encryption-{secret}".encode('utf-8')).digest())
        _fernet_instance = Fernet(derived)
        return _fernet_instance

    try:
        # Validate that the key is a valid 32-byte urlsafe base64-encoded key
        if isinstance(raw_key, str):
            raw_key_bytes = raw_key.encode('utf-8')
        else:
            raw_key_bytes = raw_key
        _fernet_instance = Fernet(raw_key_bytes)
        return _fernet_instance
    except Exception as exc:
        logger.error("Failed to initialize Fernet cipher with provided FIELD_ENCRYPTION_KEY: %s", exc)
        raise ImproperlyConfigured("Invalid FIELD_ENCRYPTION_KEY. Must be a valid 32-byte Fernet key.") from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypts a plaintext string (e.g. an API token) using Fernet symmetric encryption.
    
    Returns:
        Base64-encoded ciphertext string.
    """
    if not plaintext:
        return ''
    fernet = get_fernet()
    encrypted_bytes = fernet.encrypt(plaintext.strip().encode('utf-8'))
    return encrypted_bytes.decode('utf-8')


def decrypt_secret(ciphertext: str) -> str:
    """Decrypts a Fernet-encrypted ciphertext string.
    
    Returns:
        Plaintext string.
    Raises:
        ValueError: If decryption fails (e.g., incorrect key or corrupted ciphertext).
    """
    if not ciphertext:
        return ''
    fernet = get_fernet()
    try:
        decrypted_bytes = fernet.decrypt(ciphertext.strip().encode('utf-8'))
        return decrypted_bytes.decode('utf-8')
    except InvalidToken as exc:
        logger.error("Decryption failed: invalid token or encryption key mismatch.")
        raise ValueError("Failed to decrypt stored credential.") from exc

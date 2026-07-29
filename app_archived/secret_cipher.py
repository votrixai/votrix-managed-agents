"""Optional at-rest encryption for vault credential secret values.

Secret values are encrypted with AES-256-GCM using the key from
``VMA_ENCRYPTION_KEY`` (base64-encoded 32 bytes). Local/test mode can explicitly
allow plaintext development values; other environments fail closed. Ciphertext
strings carry the ``enc:v1:`` prefix so existing plaintext rows remain readable.
"""

import base64
import os
from typing import Any, Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

ENCRYPTED_PREFIX = "enc:v1:"
_NONCE_BYTES = 12

SECRET_VALUE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "client_secret",
    "password",
    "private_key",
    "refresh_token",
    "secret_value",
    "token",
}


def is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in SECRET_VALUE_KEYS or normalized.endswith("_token") or normalized.endswith("_api_key")


def encryption_key() -> bytes | None:
    encoded = get_settings().vma_encryption_key.strip()
    if not encoded:
        return None
    try:
        key = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("VMA_ENCRYPTION_KEY must be base64-encoded") from exc
    if len(key) != 32:
        raise ValueError("VMA_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def encrypt_secret(value: str) -> str:
    key = encryption_key()
    if key is None:
        settings = get_settings()
        if settings.vma_allow_plaintext_secrets_local and settings.app_env.lower() in {"local", "test"}:
            return value
        raise ValueError("VMA_ENCRYPTION_KEY is required before storing secrets outside local/test mode")
    if value.startswith(ENCRYPTED_PREFIX):
        return value
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)
    return ENCRYPTED_PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value.startswith(ENCRYPTED_PREFIX):
        return value
    key = encryption_key()
    if key is None:
        raise ValueError("Found an encrypted secret but VMA_ENCRYPTION_KEY is not configured")
    raw = base64.b64decode(value[len(ENCRYPTED_PREFIX) :])
    plaintext = AESGCM(key).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], None)
    return plaintext.decode("utf-8")


def encrypt_secret_values(value: Any) -> Any:
    return _walk(value, encrypt_secret)


def decrypt_secret_values(value: Any) -> Any:
    return _walk(value, decrypt_secret)


def _walk(value: Any, transform: Callable[[str], str]) -> Any:
    if isinstance(value, dict):
        return {
            key: transform(child) if is_secret_key(key) and isinstance(child, str) else _walk(child, transform)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_walk(item, transform) for item in value]
    return value

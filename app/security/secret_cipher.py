"""Versioned authenticated encryption for tenant-owned provider secrets.

AES-GCM authenticates the Organization id and OpenRouter key hash as additional
data. Moving an encrypted value to another Organization or changing its stable
provider hash therefore makes it undecryptable.
"""

from __future__ import annotations

import base64
import binascii
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = "v1"
_KEY_BYTES = 32
_NONCE_BYTES = 12
_AAD_DOMAIN = b"vma/openrouter-key"


class SecretCipherError(ValueError):
    """A persisted token could not be authenticated or decoded."""


class SecretCipherConfigurationError(ValueError):
    """The configured master key is not a valid AES-256 key."""


class UnsupportedSecretCipherVersionError(SecretCipherError):
    """The token was written by a cipher version this process cannot read."""


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    if not value:
        raise SecretCipherError("encrypted secret has an empty component")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SecretCipherError("encrypted secret is not base64url") from exc
    encoded += b"=" * (-len(encoded) % 4)
    try:
        return base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretCipherError("encrypted secret is not base64url") from exc


def _decode_master_key(value: str) -> bytes:
    encoded = value.strip()
    if not encoded:
        raise SecretCipherConfigurationError("VMA_ENCRYPTION_KEY is required")
    try:
        key = _decode_base64url(encoded)
    except SecretCipherError as exc:
        raise SecretCipherConfigurationError(
            "VMA_ENCRYPTION_KEY must be base64url encoded"
        ) from exc
    if len(key) != _KEY_BYTES:
        raise SecretCipherConfigurationError(
            "VMA_ENCRYPTION_KEY must decode to exactly 32 bytes"
        )
    return key


def _context_value(value: str, *, name: str) -> bytes:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical value")
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFFFFFF:
        raise ValueError(f"{name} is too long")
    return encoded


def _aad(*, version: str, organization_id: str, key_hash: str) -> bytes:
    organization = _context_value(organization_id, name="organization_id")
    provider_hash = _context_value(key_hash, name="key_hash")
    # Length prefixes prevent ambiguous boundaries between the two values.
    return b"".join(
        (
            _AAD_DOMAIN,
            b"\x00",
            version.encode("ascii"),
            struct.pack("!I", len(organization)),
            organization,
            struct.pack("!I", len(provider_hash)),
            provider_hash,
        )
    )


class SecretCipher:
    """AES-256-GCM encryption under one configured VMA master key.

    Persisted tokens use ``v1.<nonce>.<ciphertext-and-tag>``. Both binary
    components use unpadded base64url encoding.
    """

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
            raise SecretCipherConfigurationError(
                "SecretCipher key must contain exactly 32 bytes"
            )
        self._cipher = AESGCM(key)

    @classmethod
    def from_base64(cls, value: str) -> "SecretCipher":
        """Build from the representation used by ``VMA_ENCRYPTION_KEY``."""
        return cls(_decode_master_key(value))

    def encrypt(
        self,
        plaintext: str,
        *,
        organization_id: str,
        key_hash: str,
    ) -> str:
        if not plaintext:
            raise ValueError("plaintext secret cannot be empty")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            plaintext.encode("utf-8"),
            _aad(
                version=_VERSION,
                organization_id=organization_id,
                key_hash=key_hash,
            ),
        )
        return ".".join(
            (_VERSION, _encode_base64url(nonce), _encode_base64url(ciphertext))
        )

    def decrypt(
        self,
        token: str,
        *,
        organization_id: str,
        key_hash: str,
    ) -> str:
        try:
            version, encoded_nonce, encoded_ciphertext = token.split(".")
        except (AttributeError, ValueError) as exc:
            raise SecretCipherError("encrypted secret has an invalid envelope") from exc
        if version != _VERSION:
            raise UnsupportedSecretCipherVersionError(
                f"unsupported encrypted-secret version: {version!r}"
            )

        nonce = _decode_base64url(encoded_nonce)
        ciphertext = _decode_base64url(encoded_ciphertext)
        if len(nonce) != _NONCE_BYTES:
            raise SecretCipherError("encrypted secret has an invalid nonce")
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                _aad(
                    version=version,
                    organization_id=organization_id,
                    key_hash=key_hash,
                ),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise SecretCipherError("encrypted secret failed authentication") from exc


__all__ = [
    "SecretCipher",
    "SecretCipherConfigurationError",
    "SecretCipherError",
    "UnsupportedSecretCipherVersionError",
]

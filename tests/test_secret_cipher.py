from __future__ import annotations

import base64

import pytest

from app.security.secret_cipher import (
    SecretCipher,
    SecretCipherConfigurationError,
    SecretCipherError,
    UnsupportedSecretCipherVersionError,
)


def _base64_key(byte: int = 7) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii")


def _cipher(byte: int = 7) -> SecretCipher:
    return SecretCipher.from_base64(_base64_key(byte))


def test_round_trip_uses_a_random_versioned_envelope() -> None:
    cipher = _cipher()

    first = cipher.encrypt(
        "sk-or-v1-tenant-secret",
        organization_id="org_acme",
        key_hash="hash_123",
    )
    second = cipher.encrypt(
        "sk-or-v1-tenant-secret",
        organization_id="org_acme",
        key_hash="hash_123",
    )

    assert first.startswith("v1.")
    assert first != second
    assert "tenant-secret" not in first
    assert (
        cipher.decrypt(
            first,
            organization_id="org_acme",
            key_hash="hash_123",
        )
        == "sk-or-v1-tenant-secret"
    )


@pytest.mark.parametrize(
    ("organization_id", "key_hash"),
    [
        ("org_other", "hash_123"),
        ("org_acme", "hash_other"),
    ],
)
def test_aad_rejects_a_token_moved_to_another_identity(
    organization_id: str,
    key_hash: str,
) -> None:
    cipher = _cipher()
    token = cipher.encrypt(
        "tenant-secret",
        organization_id="org_acme",
        key_hash="hash_123",
    )

    with pytest.raises(SecretCipherError, match="authentication"):
        cipher.decrypt(
            token,
            organization_id=organization_id,
            key_hash=key_hash,
        )


def test_ciphertext_tampering_fails_authentication() -> None:
    cipher = _cipher()
    token = cipher.encrypt(
        "tenant-secret",
        organization_id="org_acme",
        key_hash="hash_123",
    )
    version, nonce, encoded_ciphertext = token.split(".")
    padding = "=" * (-len(encoded_ciphertext) % 4)
    ciphertext = bytearray(
        base64.urlsafe_b64decode((encoded_ciphertext + padding).encode("ascii"))
    )
    ciphertext[0] ^= 1
    tampered = ".".join(
        (
            version,
            nonce,
            base64.urlsafe_b64encode(ciphertext).rstrip(b"=").decode("ascii"),
        )
    )

    with pytest.raises(SecretCipherError, match="authentication"):
        cipher.decrypt(
            tampered,
            organization_id="org_acme",
            key_hash="hash_123",
        )


def test_another_master_key_cannot_decrypt_the_token() -> None:
    token = _cipher(1).encrypt(
        "tenant-secret",
        organization_id="org_acme",
        key_hash="hash_123",
    )

    with pytest.raises(SecretCipherError, match="authentication"):
        _cipher(2).decrypt(
            token,
            organization_id="org_acme",
            key_hash="hash_123",
        )


@pytest.mark.parametrize(
    "configured_key",
    ["", "not base64!", base64.urlsafe_b64encode(b"too short").decode("ascii")],
)
def test_configured_key_must_be_base64url_encoded_32_bytes(
    configured_key: str,
) -> None:
    with pytest.raises(SecretCipherConfigurationError):
        SecretCipher.from_base64(configured_key)


def test_raw_constructor_requires_exactly_32_bytes() -> None:
    with pytest.raises(SecretCipherConfigurationError):
        SecretCipher(b"short")


def test_unknown_version_is_rejected_explicitly() -> None:
    token = _cipher().encrypt(
        "tenant-secret",
        organization_id="org_acme",
        key_hash="hash_123",
    )

    with pytest.raises(UnsupportedSecretCipherVersionError):
        _cipher().decrypt(
            token.replace("v1.", "v2.", 1),
            organization_id="org_acme",
            key_hash="hash_123",
        )


@pytest.mark.parametrize("token", ["", "v1", "v1.only-two", "v1...extra"])
def test_malformed_envelopes_are_rejected(token: str) -> None:
    with pytest.raises(SecretCipherError):
        _cipher().decrypt(
            token,
            organization_id="org_acme",
            key_hash="hash_123",
        )


@pytest.mark.parametrize(
    ("organization_id", "key_hash"),
    [("", "hash_123"), (" org_acme", "hash_123"), ("org_acme", "")],
)
def test_aad_identity_must_be_canonical(
    organization_id: str,
    key_hash: str,
) -> None:
    with pytest.raises(ValueError, match="canonical"):
        _cipher().encrypt(
            "tenant-secret",
            organization_id=organization_id,
            key_hash=key_hash,
        )


def test_empty_plaintext_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        _cipher().encrypt(
            "",
            organization_id="org_acme",
            key_hash="hash_123",
        )

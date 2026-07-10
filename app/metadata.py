from typing import Any

from fastapi import HTTPException


MAX_METADATA_KEYS = 16
MAX_METADATA_KEY_CHARS = 64
MAX_METADATA_VALUE_CHARS = 512


def normalize_metadata(value: dict[str, Any] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="metadata must be an object")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _metadata_key(raw_key)
        if raw_value is None:
            raise HTTPException(status_code=422, detail="metadata values must be strings")
        normalized[key] = _metadata_value(raw_value)
    _validate_metadata_size(normalized)
    return normalized


def merge_metadata(current: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, str]:
    merged = normalize_metadata(current)
    if patch is None:
        return merged
    if not isinstance(patch, dict):
        raise HTTPException(status_code=422, detail="metadata must be an object")
    for raw_key, raw_value in patch.items():
        key = _metadata_key(raw_key)
        if raw_value is None or raw_value == "":
            merged.pop(key, None)
        else:
            merged[key] = _metadata_value(raw_value)
        _validate_metadata_size(merged)
    return merged


def _metadata_key(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="metadata keys must be strings")
    if not value:
        raise HTTPException(status_code=422, detail="metadata keys must not be empty")
    if len(value) > MAX_METADATA_KEY_CHARS:
        raise HTTPException(status_code=422, detail="metadata keys must be at most 64 characters")
    return value


def _metadata_value(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="metadata values must be strings")
    if len(value) > MAX_METADATA_VALUE_CHARS:
        raise HTTPException(status_code=422, detail="metadata values must be at most 512 characters")
    return value


def _validate_metadata_size(value: dict[str, str]) -> None:
    if len(value) > MAX_METADATA_KEYS:
        raise HTTPException(status_code=422, detail="metadata supports at most 16 keys")

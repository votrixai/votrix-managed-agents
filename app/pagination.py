import base64
import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, TypeVar

from fastapi import HTTPException

from app.models.common import ListResponse

T = TypeVar("T")

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
PAGE_CURSOR_TTL_SECONDS = 15 * 60


def paginate(
    items: Iterable[T],
    *,
    limit: int | None = None,
    page: str | None = None,
    max_limit: int = MAX_LIMIT,
) -> ListResponse[T]:
    all_items = list(items)
    page_size = _limit(limit, max_limit=max_limit)
    offset = _decode_page(page)
    sliced = all_items[offset : offset + page_size]
    next_offset = offset + page_size
    next_page = _encode_page(next_offset) if next_offset < len(all_items) else None
    return ListResponse.from_items(sliced, has_more=next_page is not None, next_page=next_page)


def paginate_by_id(
    items: Iterable[T],
    *,
    limit: int | None = None,
    after_id: str | None = None,
    before_id: str | None = None,
    max_limit: int = MAX_LIMIT,
) -> ListResponse[T]:
    all_items = list(items)
    if after_id and before_id:
        raise HTTPException(status_code=400, detail="Only one of after_id or before_id may be provided")
    page_size = _limit(limit, max_limit=max_limit)
    offset = 0
    if after_id:
        offset = _index_after(all_items, after_id)
    elif before_id:
        offset = max(0, _index_of(all_items, before_id) - page_size)
    sliced = all_items[offset : offset + page_size]
    has_more = offset + page_size < len(all_items)
    return ListResponse.from_items(sliced, has_more=has_more)


def filter_created_at(
    items: Iterable[T],
    *,
    created_at_gt: str | datetime | None = None,
    created_at_gte: str | datetime | None = None,
    created_at_lt: str | datetime | None = None,
    created_at_lte: str | datetime | None = None,
    key: Callable[[T], datetime | None] | None = None,
) -> list[T]:
    key = key or created_at_of
    gt = _parse_datetime(created_at_gt)
    gte = _parse_datetime(created_at_gte)
    lt = _parse_datetime(created_at_lt)
    lte = _parse_datetime(created_at_lte)
    filtered = []
    for item in items:
        value = key(item)
        if value is None:
            continue
        if gt is not None and value <= gt:
            continue
        if gte is not None and value < gte:
            continue
        if lt is not None and value >= lt:
            continue
        if lte is not None and value > lte:
            continue
        filtered.append(item)
    return filtered


def sort_by_created_at(items: Iterable[T], *, order: str | None = "desc") -> list[T]:
    reverse = normalize_sort_order(order) == "desc"
    return sorted(items, key=lambda item: (created_at_of(item) or datetime.min, id_of(item) or ""), reverse=reverse)


def normalize_sort_order(order: str | None, *, default: str = "desc") -> str:
    normalized = (order or default).lower()
    if normalized not in {"asc", "desc"}:
        raise HTTPException(status_code=422, detail="order must be asc or desc")
    return normalized


def created_at_of(item: Any) -> datetime | None:
    if isinstance(item, dict):
        value = item.get("created_at")
    else:
        value = getattr(item, "created_at", None)
    return _parse_datetime(value)


def id_of(item: Any) -> str | None:
    if isinstance(item, dict):
        value = item.get("id")
    else:
        value = getattr(item, "id", None)
    return value if isinstance(value, str) else None


def _index_after(items: list[T], item_id: str) -> int:
    index = _index_of(items, item_id)
    return index + 1


def _index_of(items: list[T], item_id: str) -> int:
    for index, item in enumerate(items):
        if id_of(item) == item_id:
            return index
    raise HTTPException(status_code=400, detail="Invalid pagination cursor")


def _limit(value: int | None, *, max_limit: int = MAX_LIMIT) -> int:
    if value is None:
        return DEFAULT_LIMIT
    return max(1, min(int(value), max_limit))


def _decode_page(value: str | None) -> int:
    if not value:
        return 0
    if not value.startswith("page_"):
        raise HTTPException(status_code=400, detail="Invalid page cursor")
    try:
        payload = value.removeprefix("page_")
        decoded = base64.urlsafe_b64decode(payload.encode("ascii") + b"===")
        cursor = json.loads(decoded.decode("utf-8"))
        offset = int(cursor.get("offset"))
        expires_at = _cursor_expires_at(cursor)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid page cursor") from exc
    if expires_at is not None and expires_at <= _now_epoch_seconds():
        raise HTTPException(status_code=400, detail="Expired page cursor")
    return max(0, offset)


def _encode_page(offset: int) -> str:
    payload = json.dumps(
        {"offset": offset, "expires_at": _now_epoch_seconds() + PAGE_CURSOR_TTL_SECONDS},
        separators=(",", ":"),
    ).encode("utf-8")
    return "page_" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _cursor_expires_at(cursor: dict[str, Any]) -> int | None:
    value = cursor.get("expires_at")
    if value is None:
        return None
    return int(value)


def _now_epoch_seconds() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

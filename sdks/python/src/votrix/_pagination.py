from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Generic, TypeVar

from ._models import ListEnvelope, VotrixModel

T = TypeVar("T", bound=VotrixModel)
PageLoader = Callable[[Mapping[str, Any]], Awaitable[ListEnvelope[T]]]


class AsyncPage(Generic[T]):
    def __init__(
        self,
        envelope: ListEnvelope[T],
        *,
        loader: PageLoader[T],
        base_params: Mapping[str, Any],
        cursor_param: str,
        current_cursor: str | None = None,
        seen_cursors: set[str] | None = None,
    ) -> None:
        self.data = envelope.data
        self.has_more = envelope.has_more
        self.first_id = envelope.first_id
        self.last_id = envelope.last_id
        self.next_page = envelope.next_page
        self._loader = loader
        self._base_params = dict(base_params)
        self._cursor_param = cursor_param
        self._current_cursor = current_cursor
        self._seen_cursors = set(seen_cursors or ())
        if current_cursor:
            self._seen_cursors.add(current_cursor)

    def __aiter__(self) -> AsyncIterator[T]:
        return self._iterate_all()

    async def _iterate_all(self) -> AsyncIterator[T]:
        page: AsyncPage[T] | None = self
        seen_ids: set[str] = set()
        while page is not None:
            for item in page.data:
                item_id = getattr(item, "id", None)
                if isinstance(item_id, str):
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                yield item
            page = await page.get_next_page()

    async def get_next_page(self) -> "AsyncPage[T] | None":
        if self._cursor_param == "after_id":
            cursor = self.last_id
        elif self._cursor_param == "before_id":
            cursor = self.first_id
        else:
            cursor = self.next_page
        if not self.has_more or not cursor:
            return None
        if cursor in self._seen_cursors:
            return None
        params = dict(self._base_params)
        if self._cursor_param == "after_id":
            params.pop("before_id", None)
        elif self._cursor_param == "before_id":
            params.pop("after_id", None)
        params[self._cursor_param] = cursor
        envelope = await self._loader(params)
        return AsyncPage(
            envelope,
            loader=self._loader,
            base_params=self._base_params,
            cursor_param=self._cursor_param,
            current_cursor=cursor,
            seen_cursors=self._seen_cursors,
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "data": [item.model_dump(mode="json") for item in self.data],
            "has_more": self.has_more,
            "first_id": self.first_id,
            "last_id": self.last_id,
            "next_page": self.next_page,
        }


class AsyncPaginator(Generic[T]):
    """A lazy list request that is both awaitable and directly async iterable."""

    def __init__(
        self,
        loader: PageLoader[T],
        *,
        params: Mapping[str, Any] | None = None,
        cursor_param: str = "page",
    ) -> None:
        self._loader = loader
        self._params = {key: value for key, value in dict(params or {}).items() if value is not None}
        self._cursor_param = cursor_param
        self._first_page: AsyncPage[T] | None = None

    def __await__(self):
        return self._get_first_page().__await__()

    def __aiter__(self) -> AsyncIterator[T]:
        return self._iterate()

    async def _get_first_page(self) -> AsyncPage[T]:
        if self._first_page is None:
            envelope = await self._loader(self._params)
            self._first_page = AsyncPage(
                envelope,
                loader=self._loader,
                base_params=self._params,
                cursor_param=self._cursor_param,
                current_cursor=self._params.get(self._cursor_param),
            )
        return self._first_page

    async def _iterate(self) -> AsyncIterator[T]:
        page = await self._get_first_page()
        async for item in page:
            yield item

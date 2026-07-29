"""Shared query helpers.

Paging is done by cursor rather than by offset. Both look the same until the
table is being written to while someone reads it, which is the normal case
here — sessions, events and files all grow constantly. `OFFSET 20` means "skip
twenty rows", so three rows inserted between two requests push three rows the
reader already saw onto the next page. A cursor names a row, and everything
after that row stays after it however much arrives in front.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

# What a caller gets when it does not ask, and the most it may ask for.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 1000


@dataclass(frozen=True)
class Page:
    """One page of rows, and whether the next one exists.

    `has_more` is answered by asking for one row more than the caller wanted
    and seeing whether it came back — an exact answer for the price of a row,
    where counting the whole table would cost a second scan.
    """

    items: list[Any]
    has_more: bool

    @property
    def first_id(self) -> str | None:
        return getattr(self.items[0], "id", None) if self.items else None

    @property
    def last_id(self) -> str | None:
        return getattr(self.items[-1], "id", None) if self.items else None


async def fetch_page(
    db: AsyncSession,
    stmt: Select,
    *,
    sort: Any,
    id_column: Any,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
    descending: bool = True,
) -> Page:
    """Run `stmt` as one cursor-paged read.

    `sort` is what the rows are ordered by and `id_column` breaks its ties, so
    two rows created in the same millisecond still have a definite order — a
    cursor is worthless if the row it names could move.

    `after_id` walks the way the list already reads; `before_id` walks back.
    Going back is run in reverse and then flipped, so the page is the rows
    nearest the cursor rather than the far end of everything beyond it.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    backwards = before_id is not None
    cursor = before_id if backwards else after_id

    ascending = descending == backwards
    if cursor is not None:
        anchor = select(sort).where(id_column == cursor).scalar_subquery()
        # Past the anchor, in whichever direction this page is walking.
        beyond = (sort > anchor) if ascending else (sort < anchor)
        tie = (id_column > cursor) if ascending else (id_column < cursor)
        stmt = stmt.where(or_(beyond, and_(sort == anchor, tie)))

    order = (sort.asc(), id_column.asc()) if ascending else (sort.desc(), id_column.desc())
    stmt = stmt.order_by(*order)

    result = await db.execute(stmt.limit(limit + 1))
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    if backwards:
        rows.reverse()
    return Page(items=rows, has_more=has_more)

"""Identifiers that sort into the order they were created in.

Sorting a list of ids and sorting by creation time are the same operation, so
anything that needs "these, in the order they happened" — a page of events, the
tool calls announced for one model reply — can say so with the ids it already
has, instead of carrying a second column to order by.

The layout is UUIDv7: 48 bits of millisecond timestamp first, so lexicographic
order is chronological order, then a counter, then randomness.
"""

import os
import threading
import time
import uuid

# UUIDv7 alone only orders ids from different milliseconds, and a model reply
# asking for five tools writes all five inside one. The 12 bits RFC 9562 leaves
# free after the version are used as a counter so those five still come back in
# the order they went in.
_lock = threading.Lock()
_last_ms = 0
_counter = 0
_COUNTER_BITS = 12
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1


def _timestamp_and_counter() -> tuple[int, int]:
    """The current millisecond, and this id's place within it."""
    global _last_ms, _counter
    with _lock:
        now = int(time.time() * 1000)
        if now > _last_ms:
            _last_ms = now
            # Start low rather than at zero: the room above is what a burst
            # inside one millisecond counts into.
            _counter = int.from_bytes(os.urandom(2), "big") & 0x3FF
        elif _counter < _COUNTER_MAX:
            _counter += 1
        else:
            # More than 4096 ids in one millisecond. Borrowing from the next
            # millisecond keeps them ordered; the clock catches up immediately.
            _last_ms += 1
            _counter = 0
        return _last_ms, _counter


def new_id(prefix: str) -> str:
    """Build a prefixed identifier, e.g. `sess_0198c4a1f2e07a3b...`."""
    milliseconds, counter = _timestamp_and_counter()
    value = bytearray(16)
    value[0:6] = milliseconds.to_bytes(6, "big")
    value[6:8] = (0x7000 | counter).to_bytes(2, "big")  # version 7 + counter
    value[8:16] = os.urandom(8)
    value[8] = (value[8] & 0x3F) | 0x80  # RFC 9562 variant
    return f"{prefix}_{uuid.UUID(bytes=bytes(value)).hex}"

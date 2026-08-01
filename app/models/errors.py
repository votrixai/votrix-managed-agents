"""Failures a service reports to its caller.

Services do not know about HTTP, so they raise these instead of status codes;
the router layer is what turns them into responses.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base for every failure a service raises deliberately."""


class NotFound(ServiceError):
    """No such row, or it belongs to another organization."""


class Conflict(ServiceError):
    """The request is well formed but wrong for the current state."""


class InvalidRequest(ServiceError):
    """The request cannot be interpreted under this endpoint's contract."""


class PayloadTooLarge(ServiceError):
    """The request contains a bounded payload over its documented limit."""


class MemoryPreconditionFailed(Conflict):
    """A Memory changed since the caller computed its content hash."""


class SessionBusy(ServiceError):
    """The session is mid-reply and there is no queue to hold the message.

    Deliberately says nothing about when to try again. The lease is renewed
    every forty-five seconds for as long as the worker lives, so its remainder
    is not how long the turn has left — it is only how long a *dead* worker
    would take to be noticed. A number that is right in the one case nobody
    cares about is worse than no number.
    """

    def __init__(self) -> None:
        super().__init__("The session is still working on the previous message.")


class SessionCancelled(ServiceError):
    """The session was interrupted while the agent was still producing output.

    Raised out of the emit path, which is what stops the rest of the turn from
    being written.
    """


class SandboxUnavailable(ServiceError):
    """The session's sandbox is gone. The session cannot continue."""


class MemoryStoreUnavailable(ServiceError):
    """The provider could not create, mount, or destroy a Memory Store Volume."""

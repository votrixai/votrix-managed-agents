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


class SessionBusy(ServiceError):
    """The session is mid-reply and there is no queue to hold the message.

    Carries the lease remainder so the caller can tell the client how long to
    wait — worst case, that is how long it takes a dead worker to time out.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("The session is still working on the previous message.")
        self.retry_after_seconds = retry_after_seconds


class SessionCancelled(ServiceError):
    """The session was interrupted while the agent was still producing output.

    Raised out of the emit path, which is what stops the rest of the turn from
    being written.
    """


class SandboxUnavailable(ServiceError):
    """The session's sandbox is gone. The session cannot continue."""

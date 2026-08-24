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


class ProviderRateLimited(ServiceError):
    """The container provider refused: too many at once for this account.

    Its own limit, not one of ours, and it is the account's to raise. Kept
    separate from `Conflict` because nothing about the request was wrong and
    nothing in this service's state has to change — the same call works once
    something finishes or the plan is bigger. Without it E2B's 429 arrived as
    a bare 500, which says nothing a caller can act on.
    """


class SandboxUnavailable(ServiceError):
    """The session's sandbox is gone. The session cannot continue."""


class AccountUnavailable(Conflict):
    """The Account cannot spend, so nothing can be run on its behalf.

    Distinct from a bare `Conflict` because it is the one a caller can act on:
    an Account is suspended, or was never finished being provisioned, and the
    answer is to resume it or provision it rather than to retry.
    """


class MemoryStoreUnavailable(ServiceError):
    """The provider could not create, mount, or destroy a Memory Store Volume."""

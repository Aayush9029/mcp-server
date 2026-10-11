"""What one request tells the activity log of its key. Only metadata comes here.

The REST routes and the MCP endpoint both fill in a `Trace`. A message in it is text that the server wrote
itself, or the names of the fields that are not valid: never a value from the client, and never a credential.
"""

import re
from collections.abc import Iterable
from contextvars import ContextVar, Token
from dataclasses import dataclass

from mcp_client_server.credentials import AgentPrincipal
from mcp_client_server.errors import ErrorCode
from mcp_client_server.limits import HOST_MAX
from mcp_client_server.models import ClientInfo, Surface

HOST_HEADER = "x-relaydeck-host"

_FIELDS_SHOWN = 8
# A field name can be a name that the client made up, for example an unknown field of a body.
_FIELD_NAME = re.compile(r"[A-Za-z0-9_]{1,40}(\.[A-Za-z0-9_]{1,40}){0,3}")
_FIRST_PRINTABLE, _LAST_PRINTABLE = " ", "~"


def host_name(value: str | None) -> str | None:
    """The host name that an agent sent, or None for a value that is not valid. A bad value is never an error."""
    if value is None:
        return None
    host = value.strip()
    if not 1 <= len(host) <= HOST_MAX:
        return None
    if any(not _FIRST_PRINTABLE <= character <= _LAST_PRINTABLE for character in host):
        return None
    return host


def invalid_fields(names: Iterable[str]) -> str:
    """A summary of a validation failure that has the names of the fields and not their values."""
    safe = sorted({name for name in names if _FIELD_NAME.fullmatch(name)})
    if not safe:
        return "The request is not valid."
    shown, more = ", ".join(safe[:_FIELDS_SHOWN]), len(safe) - _FIELDS_SHOWN
    return f"Invalid fields: {shown}, and {more} more." if more > 0 else f"Invalid fields: {shown}."


@dataclass(slots=True)
class Trace:
    """One HTTP request as the activity log sees it. An entry is written only if `principal` and `action` are set."""

    surface: Surface
    host: str | None
    started: float
    principal: AgentPrincipal | None = None
    at: float | None = None
    client: ClientInfo | None = None
    action: str | None = None
    code: ErrorCode | None = None
    message: str | None = None
    # A call that only waited, and came back with nothing new, has no entry.
    quiet: bool = False

    def fail(self, code: ErrorCode, message: str | None, fields: Iterable[str] | None = None) -> None:
        self.code = code
        self.message = message if fields is None else invalid_fields(fields)


_current: ContextVar[Trace | None] = ContextVar("activity_trace", default=None)


def bind(trace: Trace) -> Token[Trace | None]:
    return _current.set(trace)


def unbind(token: Token[Trace | None]) -> None:
    _current.reset(token)


def current() -> Trace | None:
    return _current.get()


def host() -> str | None:
    """The host name of the request that runs now."""
    trace = _current.get()
    return trace.host if trace else None


def failed(code: ErrorCode, message: str | None, fields: Iterable[str] | None = None) -> None:
    """Note that the request that runs now failed. Does nothing outside a request."""
    trace = _current.get()
    if trace is not None:
        trace.fail(code, message, fields)


def waited_for_nothing() -> None:
    """Note that the request that runs now only waited. Such a request has no entry."""
    trace = _current.get()
    if trace is not None:
        trace.quiet = True

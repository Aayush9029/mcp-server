from collections.abc import Generator
from contextlib import contextmanager

from mcp_client_server.credentials import AccountPrincipal, AgentPrincipal, Principal
from mcp_client_server.errors import RateLimited

RETRY_AFTER_SECONDS = 5


class WaitSlots:
    """How many calls of one credential wait at the same time in this process. One more is refused."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._waiting: dict[str, int] = {}

    def count(self, principal: Principal) -> int:
        return self._waiting.get(_owner(principal), 0)

    @contextmanager
    def take(self, principal: Principal) -> Generator[None]:
        owner = _owner(principal)
        if self._waiting.get(owner, 0) >= self._limit:
            raise RateLimited(
                f"{self._limit} calls of this key wait at the same time already. Let one of them return first.",
                RETRY_AFTER_SECONDS,
            )
        self._waiting[owner] = self._waiting.get(owner, 0) + 1
        try:
            yield
        finally:
            left = self._waiting[owner] - 1
            if left:
                self._waiting[owner] = left
            else:
                del self._waiting[owner]


def _owner(principal: Principal) -> str:
    match principal:
        case AgentPrincipal():
            return principal.key_id
        case AccountPrincipal():
            return principal.account_id

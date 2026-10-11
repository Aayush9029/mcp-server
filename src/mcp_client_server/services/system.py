import logging

from mcp_client_server.errors import StoreUnavailable
from mcp_client_server.store import Store

logger = logging.getLogger(__name__)


class SystemService:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def store_answers(self) -> bool:
        try:
            await self._store.ping()
        except StoreUnavailable as exc:
            logger.warning("health check: the store does not answer (%s)", type(exc.__cause__ or exc).__name__)
            return False
        return True

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Unix seconds with microseconds."""
        ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return round(time.time(), 6)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

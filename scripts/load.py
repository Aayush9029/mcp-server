"""Repeatable load test for a server that runs locally.

Start the server with the rate limits off, then run this script:

    RATE_LIMIT_PER_MINUTE=0 RATE_LIMIT_ACCOUNTS_PER_HOUR=0 LOG_LEVEL=WARNING uv run mcp-client-server
    uv run python scripts/load.py --url http://127.0.0.1:8000

It measures three operations and prints requests for each second and the p50, p95, and p99 latency:

    create_task   POST /v1/tasks with an API key
    list_tasks    GET /v1/tasks?limit=500 for a key that has 500 tasks
    mcp_call      POST /mcp tools/call update_task (MCP 2026-07-28)

The timed requests use a small HTTP/1.1 client on raw sockets. A full HTTP library costs more
processor time for each request than the server does, and then the test measures the client.
"""

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

OPEN_TASKS_PER_KEY = 1000
KEYS_PER_ACCOUNT = 20
MCP_VERSION = "2026-07-28"
MCP_META = {
    "io.modelcontextprotocol/protocolVersion": MCP_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "load-script", "version": "1"},
}

Headers = dict[str, str]
Target = tuple[str, int]
Reply = tuple[int, bytes]


class Connection:
    """One keep-alive HTTP/1.1 connection."""

    def __init__(self, target: Target) -> None:
        self._host, self._port = target
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def request(self, method: str, path: str, headers: Headers, body: bytes = b"") -> Reply:
        if self._writer is None or self._reader is None:
            self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        lines = [f"{method} {path} HTTP/1.1", f"Host: {self._host}:{self._port}", f"Content-Length: {len(body)}"]
        lines += [f"{name}: {value}" for name, value in headers.items()]
        self._writer.write("\r\n".join(lines).encode() + b"\r\n\r\n" + body)
        head = await self._reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                length = int(value)
        return int(head[9:12]), await self._reader.readexactly(length)

    def close(self) -> None:
        if self._writer:
            self._writer.close()


Shoot = Callable[[Connection, int, int], Awaitable[Reply]]


@dataclass
class Result:
    name: str
    requests: int
    errors: int
    seconds: float
    latencies_ms: list[float]

    def row(self) -> dict[str, Any]:
        cuts = statistics.quantiles(self.latencies_ms, n=100) if len(self.latencies_ms) > 1 else [0.0] * 99
        return {
            "operation": self.name,
            "requests": self.requests,
            "errors": self.errors,
            "rps": round(self.requests / self.seconds, 1),
            "p50_ms": round(cuts[49], 2),
            "p95_ms": round(cuts[94], 2),
            "p99_ms": round(cuts[98], 2),
        }


async def new_account(http: httpx.AsyncClient) -> Headers:
    response = await http.post("/v1/accounts", json={})
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def new_key(http: httpx.AsyncClient, account: Headers, label: str) -> Headers:
    response = await http.post("/v1/keys", json={"label": label}, headers=account)
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['secret']}"}


async def new_keys(http: httpx.AsyncClient, count: int) -> list[Headers]:
    keys: list[Headers] = []
    while len(keys) < count:
        account = await new_account(http)
        batch = min(KEYS_PER_ACCOUNT, count - len(keys))
        keys += [await new_key(http, account, f"load {number}") for number in range(batch)]
    return keys


async def run(name: str, target: Target, shoot: Shoot, total: int, concurrency: int) -> Result:
    """Send `total` requests from `concurrency` workers. `shoot` gets the worker number and the request number."""
    latencies: list[float] = []
    errors = 0
    numbers = iter(range(total))

    async def worker(index: int) -> None:
        nonlocal errors
        connection = Connection(target)
        try:
            for number in numbers:
                started = time.perf_counter()
                status, body = await shoot(connection, index, number)
                latencies.append((time.perf_counter() - started) * 1000)
                if status >= 400 or b'"isError":true' in body:
                    errors += 1
        finally:
            connection.close()

    started = time.perf_counter()
    await asyncio.gather(*(worker(index) for index in range(concurrency)))
    return Result(name, len(latencies), errors, time.perf_counter() - started, latencies)


async def create_task(http: httpx.AsyncClient, target: Target, total: int, concurrency: int) -> Result:
    keys = await new_keys(http, -(-total // OPEN_TASKS_PER_KEY))
    body = {"title": "Load test task", "description": "Made by scripts/load.py", "priority": "high"}
    payload = json.dumps(body).encode()

    async def shoot(connection: Connection, _: int, number: int) -> Reply:
        headers = {**keys[number // OPEN_TASKS_PER_KEY], "Content-Type": "application/json"}
        return await connection.request("POST", "/v1/tasks", headers, payload)

    return await run("create_task", target, shoot, total, concurrency)


async def list_tasks(http: httpx.AsyncClient, target: Target, total: int, concurrency: int) -> Result:
    key = (await new_keys(http, 1))[0]
    for number in range(500):
        body = {"title": f"Seed task {number}", "description": "x" * 200, "status": "todo" if number % 2 else "done"}
        (await http.post("/v1/tasks", json=body, headers=key)).raise_for_status()
    check = await http.get("/v1/tasks?limit=500", headers=key)
    assert len(check.json()["tasks"]) == 500, "the seed did not make 500 tasks"

    async def shoot(connection: Connection, _: int, __: int) -> Reply:
        return await connection.request("GET", "/v1/tasks?limit=500", key)

    return await run("list_tasks_500", target, shoot, total, concurrency)


async def mcp_call(http: httpx.AsyncClient, target: Target, total: int, concurrency: int) -> Result:
    key = (await new_keys(http, 1))[0]
    headers = {
        **key,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MCP_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "update_task",
    }
    # One task for each worker, so that two workers never write the same task.
    task_ids: list[str] = []
    for number in range(concurrency):
        response = await http.post("/v1/tasks", json={"title": f"MCP task {number}", "notify": False}, headers=key)
        response.raise_for_status()
        task_ids.append(response.json()["id"])

    async def shoot(connection: Connection, worker: int, number: int) -> Reply:
        arguments = {
            "task_id": task_ids[worker],
            "status": "in_progress",
            "progress": number % 100,
            "status_note": f"Step {number}",
        }
        body = {
            "jsonrpc": "2.0",
            "id": number,
            "method": "tools/call",
            "params": {"name": "update_task", "arguments": arguments, "_meta": MCP_META},
        }
        return await connection.request("POST", "/mcp", headers, json.dumps(body).encode())

    return await run("mcp_tools_call_update_task", target, shoot, total, concurrency)


SCENARIOS = {"create_task": create_task, "list_tasks": list_tasks, "mcp_call": mcp_call}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--requests", type=int, default=20_000, help="requests for create_task and mcp_call")
    parser.add_argument("--list-requests", type=int, default=3_000, help="requests for list_tasks")
    parser.add_argument("--only", choices=sorted(SCENARIOS), action="append")
    parser.add_argument("--json", action="store_true", help="print one JSON object for each operation")
    args = parser.parse_args()

    url = urlsplit(args.url)
    target = (url.hostname or "127.0.0.1", url.port or 80)
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as http:
        (await http.get("/health")).raise_for_status()
        rows: list[dict[str, Any]] = []
        for name in args.only or list(SCENARIOS):
            total = args.list_requests if name == "list_tasks" else args.requests
            rows.append((await SCENARIOS[name](http, target, total, args.concurrency)).row())

    if args.json:
        for row in rows:
            print(json.dumps(row))
        return
    print(f"{'operation':<28}{'requests':>9}{'errors':>8}{'rps':>10}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}")
    for row in rows:
        print(
            f"{row['operation']:<28}{row['requests']:>9}{row['errors']:>8}{row['rps']:>10}"
            f"{row['p50_ms']:>9}{row['p95_ms']:>9}{row['p99_ms']:>9}"
        )


if __name__ == "__main__":
    asyncio.run(main())

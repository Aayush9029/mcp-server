"""A local stand-in for the inbox routes of `docs/inbox.md`. The hook tests and the manual runs use it.

As a program, it prints its port and then serves until it is stopped:

    python tests/inbox_stub.py [PORT]

The routes under `/_stub` are not part of the contract. A manual run uses them to add a key and an item.
"""

import json
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast, override
from urllib.parse import parse_qs, urlsplit

# Parsed JSON has no static shape. The stub reads it by key, so `Any` stays inside the test code.
type Json = dict[str, Any]

PERSON: Json = {"type": "person"}
MAX_WAIT = 50.0


@dataclass
class Key:
    key_id: str
    label: str
    items: list[Json] = field(default_factory=list[Json])

    def unread(self) -> list[Json]:
        return [item for item in self.items if item["read_at"] is None]


class InboxStub:
    """The inboxes, the requests that came in, and the switches that change how the server answers."""

    def __init__(self) -> None:
        self.keys: dict[str, Key] = {}
        self.requests: list[Json] = []
        self.delay = 0.0
        self.status: int | None = None
        self.body: bytes | None = None
        self.marks_read = True
        self._changed = threading.Condition()
        self._count = 0

    def add_key(self, key: str, label: str = "Claude Code") -> Key:
        with self._changed:
            self.keys[key] = Key(f"key_{len(self.keys) + 1:04d}", label)
            return self.keys[key]

    def add_item(
        self,
        key: str,
        kind: str = "message",
        text: str = "Stop and rebase first.",
        *,
        sender: Json | None = None,
        task_id: str | None = None,
        question_id: str | None = None,
    ) -> Json:
        with self._changed:
            self._count += 1
            item: Json = {
                "id": f"inb_{self._count:04d}",
                "kind": kind,
                "from": sender or PERSON,
                "task_id": task_id,
                "question_id": question_id,
                "text": text,
                "created_at": time.time(),
                "read_at": None,
            }
            self.keys[key].items.append(item)
            self._changed.notify_all()
            return item

    def read_inbox(self, key: Key, wait: float, limit: int, *, mark_read: bool) -> Json:
        deadline = time.monotonic() + min(max(wait, 0.0), MAX_WAIT)
        with self._changed:
            while not key.unread() and (left := deadline - time.monotonic()) > 0:
                self._changed.wait(left)
            unread = key.unread()
            taken = [dict(item) for item in unread[:limit]]
            if mark_read and self.marks_read:
                for item in unread[:limit]:
                    item["read_at"] = time.time()
            return {"items": taken, "unread": len(key.unread())}

    def agents(self, caller: Key) -> Json:
        listed = [
            {"key_id": key.key_id, "label": key.label, "is_self": key is caller, "last_client": None}
            for key in self.keys.values()
        ]
        return {"agents": listed}

    def send(self, caller: Key, to: str, text: str) -> Json | None:
        targets = [secret for secret, key in self.keys.items() if to in (key.key_id, key.label) and key is not caller]
        if len(targets) != 1:
            return None
        sender: Json = {"type": "agent", "key_id": caller.key_id, "label": caller.label}
        return self.add_item(targets[0], "message", text, sender=sender)


class StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, stub: InboxStub, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.stub = stub

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    @property
    def stub(self) -> InboxStub:
        return cast("StubServer", self.server).stub

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _serve(self, method: str) -> None:
        stub, url = self.stub, urlsplit(self.path)
        size = int(self.headers.get("Content-Length") or 0)
        sent: Json = json.loads(self.rfile.read(size)) if size else {}
        if url.path.startswith("/_stub/"):
            self._answer(*self._control(url.path, sent))
            return
        authorization = self.headers.get("Authorization") or ""
        stub.requests.append(
            {
                "method": method,
                "path": self.path,
                "authorization": authorization,
                "agent": self.headers.get("User-Agent"),
                "host": self.headers.get("X-Relaydeck-Host"),
            }
        )
        time.sleep(stub.delay)
        key = stub.keys.get(authorization.removeprefix("Bearer "))
        if stub.status is not None:
            self._answer(stub.status, {"error": {"code": "stub", "message": "The stub gives this status."}})
        elif key is None:
            self._answer(401, {"error": {"code": "unauthorized", "message": "The API key is not valid."}})
        else:
            self._answer(*self._route(method, url.path, parse_qs(url.query), key, sent))

    def _route(self, method: str, path: str, query: dict[str, list[str]], key: Key, sent: Json) -> tuple[int, Json]:
        stub = self.stub
        if (method, path) == ("GET", "/v1/inbox"):
            wait = float(query.get("wait", ["0"])[0])
            limit = int(query.get("limit", ["20"])[0])
            mark_read = query.get("mark_read", ["true"])[0] != "false"
            return 200, stub.read_inbox(key, wait, limit, mark_read=mark_read)
        if (method, path) == ("GET", "/v1/agents"):
            return 200, stub.agents(key)
        if (method, path) == ("GET", "/v1/tasks"):
            return 200, {"tasks": [], "next_cursor": None}
        if (method, path) == ("POST", "/v1/messages"):
            item = stub.send(key, str(sent.get("to")), str(sent.get("text")))
            return (201, item) if item else (404, {"error": {"code": "not_found", "message": "No such agent."}})
        return 404, {"error": {"code": "not_found", "message": "The stub has no such route."}}

    def _control(self, path: str, sent: Json) -> tuple[int, Json]:
        stub = self.stub
        if path == "/_stub/keys":
            key = stub.add_key(sent["key"], sent.get("label", "Claude Code"))
            return 201, {"key_id": key.key_id, "label": key.label}
        if path == "/_stub/items":
            item = stub.add_item(
                sent["key"],
                sent.get("kind", "message"),
                sent.get("text", ""),
                sender=sent.get("from"),
                task_id=sent.get("task_id"),
                question_id=sent.get("question_id"),
            )
            return 201, item
        if path == "/_stub/state":
            inboxes = {key.label: key.items for key in stub.keys.values()}
            paths = [request["path"] for request in stub.requests]
            return 200, {"inboxes": inboxes, "requests": paths}
        return 404, {}

    def _answer(self, status: int, data: Json) -> None:
        body = self.stub.body if self.stub.body is not None and status == 200 else json.dumps(data).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # The client did not wait for the answer.
            pass


@contextmanager
def running(stub: InboxStub, port: int = 0) -> Generator[StubServer]:
    server = StubServer(stub, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    with running(InboxStub(), port) as server:
        print(server.server_address[1], flush=True)  # noqa: T201  the port is the output of the program
        threading.Event().wait()


if __name__ == "__main__":
    main()

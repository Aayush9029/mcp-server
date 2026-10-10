import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from mcp_client_server.config import Settings
from mcp_client_server.events import EventHub, frames
from mcp_client_server.events import hub as hub_module
from mcp_client_server.main import event_client
from mcp_client_server.services import Services
from tests.conftest import Api, Connect, Headers

Event = tuple[str, Any]


class Stream:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self._lines = response.aiter_lines()

    async def next(self, timeout: float = 5.0) -> Event | None:
        """The next event, ("ping", None) for a comment, or None when the server closes the stream."""
        name, data = "", ""
        async with asyncio.timeout(timeout):
            async for line in self._lines:
                if line.startswith(":"):
                    return "ping", None
                if line.startswith("event: "):
                    name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data = line.removeprefix("data: ")
                elif not line and name:
                    return name, json.loads(data)
        return None

    async def named(self, wanted: str) -> Any:
        while True:
            event = await self.next()
            assert event is not None, f"the stream ended before {wanted}"
            if event[0] == wanted:
                return event[1]


@asynccontextmanager
async def events(live_url: str, auth: Headers) -> AsyncIterator[Stream]:
    async with (
        httpx.AsyncClient(base_url=live_url, timeout=10) as http,
        http.stream("GET", "/v1/events", headers=auth) as response,
    ):
        assert response.status_code == 200, await response.aread()
        yield Stream(response)


async def account_id(api: Api, auth: Headers) -> str:
    return (await api.http.get("/v1/account", headers=auth)).json()["id"]


async def test_stream_starts_with_ready_and_sse_headers(live_url: str, api: Api) -> None:
    account = await api.account()
    async with events(live_url, account) as stream:
        assert stream.response.headers["content-type"].startswith("text/event-stream")
        assert stream.response.headers["cache-control"] == "no-cache, no-transform"
        assert stream.response.headers["x-accel-buffering"] == "no"
        assert await stream.next() == ("ready", {"account_id": await account_id(api, account)})


async def test_stream_needs_the_account_token(live_url: str, api: Api) -> None:
    account = await api.account()
    key, _ = await api.key(account)
    async with httpx.AsyncClient(base_url=live_url) as http:
        assert (await http.get("/v1/events")).status_code == 401
        assert (await http.get("/v1/events", headers={"Authorization": "Bearer mca_wrong"})).status_code == 401
        assert (await http.get("/v1/events", headers=key)).status_code == 403


async def test_task_and_key_changes_arrive_in_order(live_url: str, api: Api) -> None:
    account = await api.account()
    async with events(live_url, account) as stream:
        assert (await stream.next() or ("", None))[0] == "ready"

        key, key_info = await api.key(account, "Cursor")
        assert await stream.next() == ("key.created", {"key": key_info})

        task = await api.task(key, title="Ship it")
        assert await stream.next() == ("task.created", {"task": task})

        patched = (await api.http.patch(f"/v1/tasks/{task['id']}", json={"progress": 5}, headers=key)).json()
        assert await stream.next() == ("task.updated", {"task": patched})

        # A patch that changes nothing sends no event.
        await api.http.patch(f"/v1/tasks/{task['id']}", json={"progress": 5}, headers=key)

        renamed = (
            await api.http.patch(f"/v1/keys/{key_info['id']}", json={"label": "Cursor (work)"}, headers=account)
        ).json()
        event = await stream.next()
        assert event is not None and event[0] == "key.updated"
        assert event[1]["key"]["label"] == "Cursor (work)" and event[1]["key"]["id"] == renamed["id"]

        await api.http.delete(f"/v1/tasks/{task['id']}", headers=account)
        assert await stream.next() == ("task.deleted", {"task_id": task["id"], "key_id": key_info["id"]})

        await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)
        assert await stream.next() == ("key.deleted", {"key_id": key_info["id"]})


async def test_events_stay_inside_their_account_and_reach_each_stream(live_url: str, api: Api) -> None:
    mine = await api.account()
    theirs = await api.account()
    my_key, _ = await api.key(mine)
    their_key, _ = await api.key(theirs)
    async with events(live_url, mine) as first, events(live_url, mine) as second, events(live_url, theirs) as other:
        for stream in (first, second, other):
            await stream.named("ready")
        await api.task(their_key, title="Theirs")
        await api.task(my_key, title="Mine")
        assert (await first.named("task.created"))["task"]["title"] == "Mine"
        assert (await second.named("task.created"))["task"]["title"] == "Mine"
        assert (await other.named("task.created"))["task"]["title"] == "Theirs"
        with pytest.raises(TimeoutError):
            await other.next(timeout=0.3)


async def test_account_deleted_is_sent_and_then_the_stream_ends(live_url: str, api: Api) -> None:
    account = await api.account()
    async with events(live_url, account) as stream:
        await stream.named("ready")
        await api.http.delete("/v1/account", headers=account)
        assert await stream.next() == ("account.deleted", {})
        assert await stream.next() is None


async def test_ping_comment_and_cleanup_after_disconnect(live_url: str, api: Api, services: Services) -> None:
    services.events.ping_seconds = 0.05
    account = await api.account()
    async with events(live_url, account) as stream:
        await stream.named("ready")
        assert await stream.next() == ("ping", None)
        assert services.events.stream_count == 1
    async with asyncio.timeout(5):
        while services.events.stream_count:
            await asyncio.sleep(0.02)
    # The stream works again after the hub removed the last listener of the account.
    key, _ = await api.key(account)
    async with events(live_url, account) as stream:
        await stream.named("ready")
        await api.task(key, title="After reconnect")
        assert (await stream.named("task.created"))["task"]["title"] == "After reconnect"


async def test_slow_stream_is_closed_and_not_left_to_grow(connect: Connect, redis: Redis) -> None:
    hub = EventHub(connect(), "t:ev:", queue_size=2)
    await hub.start()
    try:
        subscription = await hub.open("acc_1")
        for number in range(5):
            await redis.publish("t:ev:acc_1", frames.frame("task.deleted", {"task_id": str(number)}))
        async with asyncio.timeout(5):
            while hub.stream_count:
                await asyncio.sleep(0.01)
        received = [item async for item in subscription.stream()]
        assert received == [frames.ready("acc_1")]
    finally:
        await hub.stop()


async def test_lost_subscriber_connection_closes_the_streams(
    settings: Settings, redis: Redis, store_url: str | None
) -> None:
    if store_url is None:
        pytest.skip("fakeredis has no connection to kill")
    subscriber = event_client(settings)
    hub = EventHub(subscriber, "t:ev:")
    await hub.start()
    try:
        subscription = await hub.open("acc_1")
        await redis.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
        async with asyncio.timeout(5):
            received = [item async for item in subscription.stream()]
        assert received == [frames.ready("acc_1")]

        async with asyncio.timeout(10):
            while True:
                try:
                    again = await hub.open("acc_1")
                    break
                except (ConnectionError, TimeoutError, OSError):
                    await asyncio.sleep(0.1)
        await redis.publish("t:ev:acc_1", frames.account_deleted())
        async with asyncio.timeout(5):
            assert [item async for item in again.stream()] == [frames.ready("acc_1"), frames.account_deleted()]
    finally:
        await hub.stop()
        await subscriber.aclose()


async def test_quiet_subscriber_connection_gets_a_ping_and_a_dead_one_is_reset(
    connect: Connect, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "_LIVENESS_SECONDS", 0.05)
    subscriber = connect()
    hub = EventHub(subscriber, "t:ev:")
    await hub.start()
    try:
        subscription = await hub.open("acc_1")
        # Ten quiet periods. The answers to the pings keep the stream open.
        await asyncio.sleep(0.5)
        assert hub.stream_count == 1

        async def no_ping(self: PubSub) -> None:
            pass

        monkeypatch.setattr(PubSub, "ping", no_ping)
        async with asyncio.timeout(5):
            assert [item async for item in subscription.stream()] == [frames.ready("acc_1")]
    finally:
        await hub.stop()
        await subscriber.aclose()

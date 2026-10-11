"""GET /v1/events on a real socket: the frames a client sees, and the cleanup after it leaves."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI

from mcp_client_server.events import EventHub, frames
from mcp_client_server.ids import AccountId
from mcp_client_server.services import Services
from mcp_client_server.store import KeySpace, RedisSubscriber, event_client
from tests.support import Api, FakeClock, Headers, Json, RawStore

type Event = tuple[str, Json | None]


class Stream:
    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self._lines = response.aiter_lines()

    async def next(self) -> Event | None:
        """The next event, or None when the server closes the stream."""
        name, data = "", ""
        async with asyncio.timeout(5):
            async for line in self._lines:
                if line.startswith("event: "):
                    name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data = line.removeprefix("data: ")
                elif not line and name:
                    return name, json.loads(data)
        return None


@pytest.fixture
async def live_url(app: FastAPI) -> AsyncIterator[str]:
    """The app on a real socket. The ASGI transport of the HTTP client cannot stream."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110  uvicorn has no event for "started"
        await asyncio.sleep(0.005)
    port: int = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


@asynccontextmanager
async def events(live_url: str, auth: Headers) -> AsyncGenerator[Stream]:
    async with (
        httpx2.AsyncClient(base_url=live_url, timeout=10) as http,
        http.stream("GET", "/v1/events", headers=auth) as response,
    ):
        assert response.status_code == 200, await response.aread()
        stream = Stream(response)
        ready = await stream.next()
        assert ready is not None
        assert ready[0] == "ready"
        yield stream


async def test_stream_starts_with_ready_and_has_the_sse_headers(live_url: str, api: Api) -> None:
    account = await api.account()
    async with (
        httpx2.AsyncClient(base_url=live_url) as http,
        http.stream("GET", "/v1/events", headers=account) as response,
    ):
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"
        assert await Stream(response).next() == ("ready", {"account_id": await api.account_id(account)})


async def test_each_change_arrives_as_the_event_of_the_contract_in_order(live_url: str, api: Api) -> None:
    account = await api.account()
    async with events(live_url, account) as stream:
        key, key_info = await api.key(account, "Cursor")
        assert await stream.next() == ("key.created", {"key": key_info})

        task = await api.task(key, title="Ship it")
        assert await stream.next() == ("task.created", {"task": task})

        patched = await api.patch(key, task["id"], progress=5)
        assert await stream.next() == ("task.updated", {"task": patched})

        # A patch that changes nothing sends no event: the next event is the one of the rename.
        await api.patch(key, task["id"], progress=5)
        renamed = (
            await api.http.patch(f"/v1/keys/{key_info['id']}", json={"label": "Cursor (wörk)"}, headers=account)
        ).json()
        assert await stream.next() == ("key.updated", {"key": renamed})

        await api.http.delete(f"/v1/tasks/{task['id']}", headers=account)
        assert await stream.next() == ("task.deleted", {"task_id": task["id"], "key_id": key_info["id"]})

        await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)
        assert await stream.next() == ("key.deleted", {"key_id": key_info["id"]})


async def test_events_stay_in_their_account_and_reach_each_of_its_streams(live_url: str, api: Api) -> None:
    mine, my_key, _ = await api.agent()
    theirs, their_key, _ = await api.agent()
    async with events(live_url, mine) as first, events(live_url, mine) as second, events(live_url, theirs) as other:
        await api.task(their_key, title="Theirs")
        await api.task(my_key, title="Mine")
        await api.task(their_key, title="Theirs again")

        for stream in (first, second):
            event = await stream.next()
            assert event is not None
            assert event[1] is not None
            assert event[1]["task"]["title"] == "Mine"
        titles = [event[1]["task"]["title"] for event in (await other.next(), await other.next()) if event and event[1]]
        assert titles == ["Theirs", "Theirs again"]


async def test_account_deleted_is_sent_and_then_the_server_closes_the_stream(live_url: str, api: Api) -> None:
    account = await api.account()
    async with events(live_url, account) as stream:
        await api.http.delete("/v1/account", headers=account)
        assert await stream.next() == ("account.deleted", {})
        assert await stream.next() is None


async def test_a_client_that_leaves_frees_its_stream_and_can_connect_again(
    live_url: str, api: Api, services: Services
) -> None:
    account, key, _ = await api.agent()
    async with events(live_url, account):
        assert services.events.stream_count == 1
    async with asyncio.timeout(5):
        while services.events.stream_count:  # noqa: ASYNC110  the server sees the closed socket a moment later
            await asyncio.sleep(0.005)

    async with events(live_url, account) as stream:
        await api.task(key, title="After the reconnect")
        event = await stream.next()
        assert event is not None
        assert event[0] == "task.created"


async def test_a_killed_subscriber_connection_closes_the_streams_and_the_hub_connects_again(
    raw: RawStore, store_url: str | None, clock: FakeClock
) -> None:
    if store_url is None:
        pytest.skip("fakeredis has no connection to kill; test_outage.py covers the loss of the store")
    account_id = AccountId("acc_1")
    keys = KeySpace("t")
    subscriber = event_client(store_url)
    hub = EventHub(lambda: RedisSubscriber(subscriber), keys, clock)
    await hub.start()
    try:
        subscription = await hub.open(account_id)
        await raw.kill_subscribers()
        async with asyncio.timeout(5):
            assert [frame async for frame in subscription.stream()] == [frames.ready(account_id)]

        clock.advance(1.0)
        async with asyncio.timeout(5):
            again = await hub.open(account_id)
            await raw.publish(keys.channel(account_id), frames.account_deleted())
            assert [frame async for frame in again.stream()] == [frames.ready(account_id), frames.account_deleted()]
    finally:
        await hub.stop()
        await subscriber.aclose()

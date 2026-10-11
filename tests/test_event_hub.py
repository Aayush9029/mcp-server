"""The event hub against a pub/sub connection in memory: fan-out, slow streams, and lost connections."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

import pytest

from mcp_client_server.errors import StoreUnavailable
from mcp_client_server.events import EventHub, HubTiming, Subscription, frames
from mcp_client_server.ids import AccountId, KeyId
from mcp_client_server.store import KeySpace
from tests.support import SIGNAL_SECONDS, FakeBus, FakeClock, settle

MINE = AccountId("acc_mine")
THEIRS = AccountId("acc_theirs")
KEYS = KeySpace("t")
LIVENESS = 20.0
# Each `open` sleeps for this limit on the clock while it waits for the confirmation of the store.
CONFIRM_LIMIT = 5.0

type StartHub = Callable[..., EventHub]


def event(number: int) -> str:
    return frames.key_deleted(KeyId(f"key_{number}"))


@pytest.fixture
def bus(clock: FakeClock) -> FakeBus:
    return FakeBus(clock)


@pytest.fixture
async def start_hub(bus: FakeBus, clock: FakeClock) -> AsyncIterator[StartHub]:
    hubs: list[EventHub] = []

    def start(*, queue_size: int = 16, **timing: float) -> EventHub:
        hub = EventHub(bus.connect, KEYS, clock, timing=HubTiming(**timing), queue_size=queue_size)
        hubs.append(hub)
        return hub

    yield start
    for hub in hubs:
        await hub.stop()


@pytest.fixture
async def hub(start_hub: StartHub) -> EventHub:
    hub = start_hub()
    await hub.start()
    return hub


async def take(subscription: Subscription, count: int) -> list[str]:
    """The next `count` frames of a stream."""
    received: list[str] = []
    stream = subscription.stream()
    async with asyncio.timeout(SIGNAL_SECONDS):
        async for frame in stream:
            received.append(frame)
            if len(received) == count:
                break
    return received


async def rest(subscription: Subscription) -> list[str]:
    """All frames until the server closes the stream."""
    async with asyncio.timeout(SIGNAL_SECONDS):
        return [frame async for frame in subscription.stream()]


async def test_ready_comes_first_and_then_the_events_of_the_account_in_order(hub: EventHub, bus: FakeBus) -> None:
    mine = await hub.open(MINE)
    theirs = await hub.open(THEIRS)
    bus.publish(KEYS.channel(THEIRS), event(9))
    bus.publish(KEYS.channel(MINE), event(1))
    bus.publish(KEYS.channel(MINE), event(2))

    assert await take(mine, 3) == [frames.ready(MINE), event(1), event(2)]
    assert await take(theirs, 2) == [frames.ready(THEIRS), event(9)]


async def test_each_stream_of_an_account_gets_each_event(hub: EventHub, bus: FakeBus) -> None:
    first = await hub.open(MINE)
    second = await hub.open(MINE)
    assert len(bus.current.channels) == 2
    bus.publish(KEYS.channel(MINE), event(1))

    assert await take(first, 2) == [frames.ready(MINE), event(1)]
    assert await take(second, 2) == [frames.ready(MINE), event(1)]


async def test_the_stream_is_ready_only_after_the_store_confirms_the_subscription(
    start_hub: StartHub, bus: FakeBus, clock: FakeClock
) -> None:
    hub = start_hub()
    await hub.start()
    bus.confirms = False
    opening = asyncio.create_task(hub.open(MINE))
    # The hub waits 5 seconds of its clock for the confirmation. No real time is in this limit.
    await clock.sleeping(5.0)
    assert not opening.done()
    clock.advance(5.0)
    with pytest.raises(StoreUnavailable):
        await opening
    assert hub.stream_count == 0

    bus.confirms = True
    assert await take(await hub.open(THEIRS), 1) == [frames.ready(THEIRS)]


async def test_a_stream_that_reads_too_slowly_is_closed_and_not_left_to_grow(start_hub: StartHub, bus: FakeBus) -> None:
    hub = start_hub(queue_size=2)
    await hub.start()
    slow = await hub.open(MINE)
    for number in range(5):
        bus.publish(KEYS.channel(MINE), event(number))
    await settle()

    assert hub.stream_count == 0
    assert await rest(slow) == [frames.ready(MINE)]


async def test_account_deleted_is_sent_and_then_the_stream_ends(hub: EventHub, bus: FakeBus) -> None:
    subscription = await hub.open(MINE)
    bus.publish(KEYS.channel(MINE), event(1))
    bus.publish(KEYS.channel(MINE), frames.account_deleted())
    bus.publish(KEYS.channel(MINE), event(2))

    assert await rest(subscription) == [frames.ready(MINE), event(1), frames.account_deleted()]
    assert hub.stream_count == 0


async def test_account_deleted_reaches_a_stream_whose_queue_it_fills(start_hub: StartHub, bus: FakeBus) -> None:
    hub = start_hub(queue_size=2)
    await hub.start()
    subscription = await hub.open(MINE)
    bus.publish(KEYS.channel(MINE), event(1))
    bus.publish(KEYS.channel(MINE), frames.account_deleted())
    await settle()

    assert hub.stream_count == 0
    assert (await rest(subscription))[-1] == frames.account_deleted()


async def test_a_silent_stream_gets_a_ping_comment(start_hub: StartHub, bus: FakeBus) -> None:
    hub = start_hub(ping_seconds=0.01)
    await hub.start()
    subscription = await hub.open(MINE)
    assert await take(subscription, 3) == [frames.ready(MINE), ": ping\n\n", ": ping\n\n"]


async def test_the_channel_is_unsubscribed_when_its_last_stream_closes(hub: EventHub, bus: FakeBus) -> None:
    first = await hub.open(MINE)
    second = await hub.open(MINE)
    channel = KEYS.channel(MINE)

    first.close()
    await settle()
    assert channel in bus.current.channels
    second.close()
    await settle()
    assert channel not in bus.current.channels
    assert hub.stream_count == 0

    bus.publish(channel, event(1))
    again = await hub.open(MINE)
    bus.publish(channel, event(2))
    assert await take(again, 2) == [frames.ready(MINE), event(2)]


async def test_a_lost_connection_closes_every_stream_and_the_hub_connects_again(
    hub: EventHub, bus: FakeBus, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp_client_server.events.hub")
    mine = await hub.open(MINE)
    theirs = await hub.open(THEIRS)
    lost = bus.current

    bus.break_connection()
    assert await rest(mine) == [frames.ready(MINE)]
    assert await rest(theirs) == [frames.ready(THEIRS)]
    # The hub sleeps before its next try. The clock moves only after that sleep started.
    await clock.sleeping(1.0)
    assert lost.closed
    assert [seconds for seconds in clock.sleeps if seconds != CONFIRM_LIMIT] == [1.0]

    clock.advance(1.0)
    again = await hub.open(MINE)
    assert bus.current is not lost
    bus.publish(KEYS.channel(MINE), event(1))
    assert await take(again, 2) == [frames.ready(MINE), event(1)]
    assert [(record.levelname, record.getMessage().split(" (")[0]) for record in caplog.records] == [
        ("WARNING", "event subscriber lost its connection"),
        ("INFO", "event subscriber is connected again"),
    ]


async def test_while_the_store_is_down_a_stream_cannot_open_and_the_log_has_one_warning(
    hub: EventHub, bus: FakeBus, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="mcp_client_server.events.hub")
    bus.down = True
    bus.break_connection()
    for _ in range(5):
        await clock.sleeping(1.0)
        with pytest.raises(StoreUnavailable):
            await hub.open(MINE)
        clock.advance(1.0)
    await clock.sleeping(1.0)
    assert hub.stream_count == 0
    assert len(caplog.records) == 1

    bus.down = False
    clock.advance(1.0)
    assert await take(await hub.open(MINE), 1) == [frames.ready(MINE)]


async def test_a_quiet_connection_gets_a_ping_and_one_with_no_answer_is_reset(
    hub: EventHub, bus: FakeBus, clock: FakeClock
) -> None:
    subscription = await hub.open(MINE)
    connection = bus.current

    # Ten quiet periods. The answers to the pings keep the connection.
    for _ in range(10):
        bus.stay_quiet()
        await settle()
    assert (connection.pings, hub.stream_count, bus.current) == (10, 1, connection)

    bus.answers_pings = False
    for _ in range(4):
        bus.stay_quiet()
        await settle()
    assert connection.closed
    assert await rest(subscription) == [frames.ready(MINE)]
    assert clock.monotonic() == pytest.approx(14 * LIVENESS)


async def test_stop_closes_the_streams_and_the_connection(start_hub: StartHub, bus: FakeBus) -> None:
    hub = start_hub()
    await hub.start()
    subscription = await hub.open(MINE)
    await hub.stop()

    assert bus.current.closed
    assert await rest(subscription) == [frames.ready(MINE)]

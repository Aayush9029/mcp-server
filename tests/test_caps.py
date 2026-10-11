"""The caps of one server process: streams for each account, and calls that wait for each key."""

import asyncio

from mcp_client_server.limits import Limits
from tests.conftest import Launch
from tests.support import SIGNAL_SECONDS, Api, Headers, Json, error, mcp_client, settle, structured, tool_text

CAP_MESSAGE = "wait at the same time already"


def waiting_inbox(api: Api, key: Headers, wait: int = 50) -> asyncio.Task[Json]:
    return asyncio.create_task(api.inbox(key, wait=wait))


async def test_one_stream_above_the_limit_ends_the_oldest_stream_of_the_account(launch: Launch) -> None:
    async with launch(limits=Limits(max_streams_per_account=2)) as running:
        account_id = await running.api.account_id(await running.api.account())
        events = running.services.events
        first = await events.open(account_id, client_stream=True)
        second = await events.open(account_id, client_stream=True)
        third = await events.open(account_id, client_stream=True)
        try:
            async with asyncio.timeout(SIGNAL_SECONDS):
                assert [frame async for frame in first.stream()] == [
                    f'event: ready\ndata: {{"account_id":"{account_id}"}}\n\n'
                ]
            assert events.stream_count == 2
        finally:
            second.close()
            third.close()


async def test_a_key_has_a_limit_of_calls_that_wait_at_the_same_time_over_rest(launch: Launch) -> None:
    async with launch(limits=Limits(max_waits_per_key=2)) as running:
        api, clock = running.api, running.clock
        account, key, _ = await api.agent()
        _, other_key, _ = await api.agent("Other")
        question = await api.ask(key)

        held = [waiting_inbox(api, key), waiting_inbox(api, key)]
        await clock.sleeping(50, 2)

        # The limit counts the inbox waits and the question waits of the key together.
        refused = await api.http.get("/v1/inbox?wait=10", headers=key)
        problem = error(refused, 429, "rate_limited")
        assert CAP_MESSAGE in problem["message"]
        assert refused.headers["retry-after"] == "5"
        error(await api.http.get(f"/v1/questions/{question['id']}?wait=10", headers=key), 429, "rate_limited")
        # A call that does not wait, and a different key, are not affected.
        assert (await api.http.get("/v1/inbox", headers=key)).status_code == 200
        assert (await api.http.get(f"/v1/questions/{question['id']}", headers=key)).status_code == 200
        # Not 5: the event hub also sleeps 5 seconds while it waits for the store to confirm a subscription.
        other = waiting_inbox(api, other_key, wait=7)
        await clock.sleeping(7)
        assert not other.done()

        clock.advance(50)
        await asyncio.wait_for(asyncio.gather(*held, other), SIGNAL_SECONDS)
        # The slots are free again.
        again = waiting_inbox(api, key, wait=7)
        await clock.sleeping(7)
        clock.advance(7)
        assert (await asyncio.wait_for(again, SIGNAL_SECONDS))["items"] == []
        assert (await api.http.get("/v1/account", headers=account)).status_code == 200


async def test_the_wait_tools_give_a_corrective_error_above_the_limit_and_ask_user_still_asks(
    launch: Launch,
) -> None:
    async with launch(limits=Limits(max_waits_per_key=1)) as running:
        api, clock = running.api, running.clock
        _, key, _ = await api.agent()
        question = await api.ask(key)
        held = waiting_inbox(api, key)
        await clock.sleeping(50)

        async with mcp_client(running.app, key) as client:
            inbox = await client.call_tool("check_inbox", {"wait_seconds": 10})
            assert inbox.is_error is True
            assert CAP_MESSAGE in tool_text(inbox)
            assert "Try again in 5 s." in tool_text(inbox)
            answer = await client.call_tool("get_answer", {"question_id": question["id"], "wait_seconds": 10})
            assert answer.is_error is True
            assert CAP_MESSAGE in tool_text(answer)
            asked = structured(await client.call_tool("ask_user", {"prompt": "Deploy?", "wait_seconds": 10}))
            assert asked["status"] == "pending"
            # A call with no wait is fine.
            assert structured(await client.call_tool("check_inbox", {}))["items"] == []

        clock.advance(50)
        await asyncio.wait_for(held, SIGNAL_SECONDS)
        await settle()

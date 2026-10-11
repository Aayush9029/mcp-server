"""A request body above the limit is refused before it fills the memory of a worker."""

from collections.abc import AsyncIterator

import httpx2

from mcp_client_server.limits import MAX_BODY_BYTES
from tests.support import Api, error

TOO_LARGE = 413


async def test_a_body_with_a_content_length_above_the_limit_gives_413_before_the_route(
    client: httpx2.AsyncClient, api: Api
) -> None:
    _, key, _ = await api.agent()
    body = b'{"title": "' + b"x" * MAX_BODY_BYTES + b'"}'

    response = await client.post("/v1/tasks", content=body, headers={**key, "Content-Type": "application/json"})
    problem = error(response, TOO_LARGE, "bad_request")
    assert "1024 KB" in problem["message"]
    assert (await client.get("/v1/tasks", headers=key)).json()["tasks"] == []
    # No credential is necessary for the refusal, so an unknown caller cannot make the server read a large body.
    anonymous = await client.post("/v1/accounts", content=body, headers={"Content-Type": "application/json"})
    error(anonymous, TOO_LARGE, "bad_request")


async def test_a_body_that_comes_in_chunks_is_refused_when_it_passes_the_limit(
    client: httpx2.AsyncClient, api: Api
) -> None:
    _, key, _ = await api.agent()
    chunk = b"x" * 65_536
    sent = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal sent
        yield b'{"title": "'
        for _ in range(MAX_BODY_BYTES // len(chunk) + 2):
            sent += len(chunk)
            yield chunk
        yield b'"}'

    response = await client.post("/v1/tasks", content=chunks(), headers={**key, "Content-Type": "application/json"})
    error(response, TOO_LARGE, "bad_request")
    # The read stopped at the limit: the chunks after it were not taken.
    assert sent <= MAX_BODY_BYTES + 2 * len(chunk)


async def test_a_body_at_the_limit_still_reaches_the_route(client: httpx2.AsyncClient, api: Api) -> None:
    _, key, _ = await api.agent()
    padding = "x" * (MAX_BODY_BYTES - 100)
    response = await client.post("/v1/tasks", json={"title": "ok", "description": padding}, headers=key)
    assert response.status_code == 422, response.text
    assert "description" in response.json()["error"]["fields"]


async def test_the_mcp_endpoint_has_the_same_limit(client: httpx2.AsyncClient, api: Api) -> None:
    _, key, _ = await api.agent()
    body = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pad": "' + b"x" * MAX_BODY_BYTES + b'"}}'
    response = await client.post("/mcp", content=body, headers={**key, "Content-Type": "application/json"})
    error(response, TOO_LARGE, "bad_request")

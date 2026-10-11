"""The APNs client against a fake HTTP layer: headers, the provider token, and each kind of answer."""

import json

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from mcp_client_server.config import ApnsConfig
from mcp_client_server.ids import DeviceId, TaskId
from mcp_client_server.push import ApnsClient, PushRequest, PushResult
from mcp_client_server.push import payloads as p
from mcp_client_server.push.payloads import TokenRef
from tests.support import ACTIVITY_TOKEN, APNS_TOKEN, FakeClock, Json, make_task

LIVE_TOPIC = "com.example.app.push-type.liveactivity"


def alert(**fields: object) -> PushRequest:
    base: dict[str, object] = {
        "token": APNS_TOKEN,
        "environment": "production",
        "topic": "com.example.app",
        "push_type": "alert",
        "priority": 10,
        "payload": p.visible("task.updated", make_task(), "Claude Code", "Task updated"),
        "ttl_seconds": 3600,
        "ref": TokenRef("apns_token", DeviceId("dev_1")),
        "collapse_id": "tsk_01jb2m9x4e7q",
    }
    return PushRequest(**{**base, **fields})  # pyright: ignore[reportArgumentType]  the test replaces fields by name


class FakeApns:
    """The HTTP side of APNs. It answers with the replies that the test queued, then with 200."""

    def __init__(self, *replies: tuple[int, Json] | Exception) -> None:
        self.requests: list[httpx2.Request] = []
        self._replies = list(replies)
        self.clock = FakeClock(start=1_000_000.0, tick=0.0)
        private = ec.generate_private_key(ec.SECP256R1())
        self.public_key = private.public_key()
        pem = private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        self.client = ApnsClient(ApnsConfig(pem, "KEY123", "TEAM456"), self.clock, http)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply: tuple[int, Json] | Exception = self._replies.pop(0) if self._replies else (200, {})
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return httpx2.Response(status, json=body) if body else httpx2.Response(status)

    def provider_token(self, index: int = -1) -> str:
        scheme, token = self.requests[index].headers["authorization"].split(" ")
        assert scheme == "bearer"
        return token


async def test_alert_request_has_the_apns_headers_and_a_signed_provider_token() -> None:
    apns = FakeApns()
    result = await apns.client.send(alert())

    assert result == PushResult(200)
    sent = apns.requests[0]
    assert str(sent.url) == f"https://api.push.apple.com/3/device/{APNS_TOKEN}"
    assert {name: sent.headers[name] for name in sent.headers if name.startswith("apns-")} == {
        "apns-topic": "com.example.app",
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": str(1_000_000 + 3600),
        "apns-collapse-id": "tsk_01jb2m9x4e7q",
    }
    assert json.loads(sent.content)["aps"]["alert"]["title"] == "Claude Code"
    claims = jwt.decode(apns.provider_token(), apns.public_key, algorithms=["ES256"], options={"verify_iat": False})
    assert claims == {"iss": "TEAM456", "iat": 1_000_000}
    assert jwt.get_unverified_header(apns.provider_token()) == {"alg": "ES256", "kid": "KEY123", "typ": "JWT"}


async def test_sandbox_and_live_activity_requests() -> None:
    apns = FakeApns()
    await apns.client.send(
        alert(push_type="background", priority=5, payload=p.silent("task.updated", make_task()), environment="sandbox")
    )
    await apns.client.send(
        alert(
            token=ACTIVITY_TOKEN,
            push_type="liveactivity",
            topic=LIVE_TOPIC,
            payload=p.activity_update(make_task(), 5.0),
            collapse_id=None,
            ref=TokenRef("activity", DeviceId("dev_1"), TaskId("tsk_1")),
        )
    )

    background, activity = apns.requests
    assert str(background.url) == f"https://api.sandbox.push.apple.com/3/device/{APNS_TOKEN}"
    assert (background.headers["apns-push-type"], background.headers["apns-priority"]) == ("background", "5")
    assert json.loads(background.content)["aps"] == {"content-available": 1}
    assert str(activity.url) == f"https://api.push.apple.com/3/device/{ACTIVITY_TOKEN}"
    assert (activity.headers["apns-topic"], activity.headers["apns-push-type"]) == (LIVE_TOPIC, "liveactivity")
    assert "apns-collapse-id" not in activity.headers


async def test_provider_token_is_reused_for_fifty_minutes_and_then_signed_again() -> None:
    apns = FakeApns()
    await apns.client.send(alert())
    apns.clock.advance(49 * 60)
    await apns.client.send(alert())
    assert apns.provider_token(0) == apns.provider_token(1)

    apns.clock.advance(2 * 60)
    await apns.client.send(alert())
    assert apns.provider_token(2) != apns.provider_token(1)
    claims = jwt.decode(apns.provider_token(2), apns.public_key, algorithms=["ES256"], options={"verify_iat": False})
    assert claims["iat"] == 1_000_000 + 51 * 60


@pytest.mark.parametrize(
    ("status", "reason", "dead"),
    [
        (410, "Unregistered", True),
        (410, "ExpiredToken", True),
        (400, "BadDeviceToken", True),
        (400, "BadTopic", False),
        (400, "DeviceTokenNotForTopic", False),
        (403, "InvalidProviderToken", False),
        (429, "TooManyRequests", False),
        (500, "InternalServerError", False),
        (503, "ServiceUnavailable", False),
    ],
)
async def test_only_410_and_bad_device_token_mean_that_the_token_is_dead(status: int, reason: str, dead: bool) -> None:
    apns = FakeApns((status, {"reason": reason}))
    result = await apns.client.send(alert())
    assert (result.status, result.reason, result.token_is_dead, result.ok) == (status, reason, dead, False)
    assert len(apns.requests) == 1


async def test_an_error_answer_that_is_not_json_has_no_reason() -> None:
    def gateway(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(502, text="<html>Bad Gateway</html>")

    apns = FakeApns()
    client = ApnsClient(
        ApnsConfig(apns_key(), "K", "T"), apns.clock, httpx2.AsyncClient(transport=httpx2.MockTransport(gateway))
    )
    assert await client.send(alert()) == PushResult(502, None)


def apns_key() -> str:
    private = ec.generate_private_key(ec.SECP256R1())
    return private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def test_expired_provider_token_is_signed_again_and_the_push_is_sent_one_more_time() -> None:
    apns = FakeApns((403, {"reason": "ExpiredProviderToken"}))
    apns.clock.advance(1)
    result = await apns.client.send(alert())

    assert result.ok
    assert len(apns.requests) == 2
    assert apns.provider_token(0) != apns.provider_token(1)


async def test_a_second_expired_answer_is_returned_and_not_tried_a_third_time() -> None:
    expired = (403, {"reason": "ExpiredProviderToken"})
    apns = FakeApns(expired, expired)
    result = await apns.client.send(alert())
    assert (result.status, result.reason, len(apns.requests)) == (403, "ExpiredProviderToken", 2)


async def test_a_closed_connection_gets_one_more_try() -> None:
    apns = FakeApns(httpx2.RemoteProtocolError("the server closed the connection"))
    assert (await apns.client.send(alert())).ok
    assert len(apns.requests) == 2

    down = FakeApns(httpx2.ConnectError("refused"), httpx2.ConnectTimeout("slow"))
    failed = await down.client.send(alert())
    assert (failed.status, failed.reason, failed.token_is_dead, failed.ok) == (0, "ConnectTimeout", False, False)
    assert len(down.requests) == 2


def test_a_key_that_cannot_sign_stops_the_startup() -> None:
    with pytest.raises(Exception, match=r"(?i)key|pem"):
        ApnsClient(ApnsConfig("not a key", "K", "T"), FakeClock())


async def test_a_widget_push_has_the_widgets_type_and_no_priority_header() -> None:
    apns = FakeApns()
    await apns.client.send(
        alert(
            push_type="widgets",
            topic="com.example.app.push-type.widgets",
            priority=None,
            payload=p.widget_reload(),
            collapse_id=None,
            ref=TokenRef("widget_push_token", DeviceId("dev_1")),
        )
    )
    sent = apns.requests[0]
    assert {name: sent.headers[name] for name in sent.headers if name.startswith("apns-")} == {
        "apns-topic": "com.example.app.push-type.widgets",
        "apns-push-type": "widgets",
        "apns-expiration": str(1_000_000 + 3600),
    }
    assert json.loads(sent.content) == {"aps": {"content-changed": True}}

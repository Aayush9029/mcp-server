"""App integrity: the App Attest attestation of a new account, and the app code (TOTP) of the app's requests."""

import base64
import hashlib
import json
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError

from mcp_client_server import appattest, totp
from mcp_client_server.appattest import AttestationError, verify_attestation
from tests.conftest import Launch, TestSettings
from tests.fake_appattest import APP_ID, Attest, FakeAppAttest
from tests.support import Api, FakeClock, Json, RawStore, Running, bearer, error

SAMPLE: dict[str, str] = json.loads((Path(__file__).parent / "data" / "apple_attestation_sample.json").read_text())
RFC_SEED = b"12345678901234567890123456789012"
BASE32_SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
KEY = totp.decode_secret(BASE32_SEED)
# The start of a 30-second step.
STEP_START = 1_760_000_010.0

type Attested = Callable[..., AbstractAsyncContextManager[tuple[Running, FakeAppAttest]]]


@pytest.fixture
def attested(launch: Launch, clock: FakeClock) -> Attested:
    """An application with the App Attest check on, and the fake Apple service that it trusts."""

    @asynccontextmanager
    async def start(**changes: object) -> AsyncGenerator[tuple[Running, FakeAppAttest]]:
        apple = FakeAppAttest(clock.time)
        settings = {"app_attest_app_ids": f"{APP_ID}, ABCDE12345.com.example.second", **changes}
        async with launch(attest_roots=[apple.root], **settings) as running:
            yield running, apple

    return start


async def challenge(http: httpx2.AsyncClient) -> str:
    response = await http.post("/v1/attest/challenge")
    assert response.status_code == 200, response.text
    assert response.json()["expires_in"] == 300
    text: str = response.json()["challenge"]
    return text


async def create(http: httpx2.AsyncClient, attestation: Json | None) -> httpx2.Response:
    body = {} if attestation is None else {"attestation": attestation}
    return await http.post("/v1/accounts", json=body)


def refused(response: httpx2.Response, code: str = "attestation_invalid") -> None:
    error(response, 401, code)
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_valid_attestation_makes_the_account_and_the_key_is_kept_with_it(
    attested: Attested, raw: RawStore
) -> None:
    async with attested() as (running, apple):
        http = running.http
        parts = Attest(await challenge(http))
        response = await create(http, apple.attest(parts))
        assert response.status_code == 201, response.text
        account = response.json()["account"]
        assert (await http.get("/v1/account", headers=bearer(response.json()["token"]))).json() == account

        numbers = parts.key.public_key().public_numbers()
        point = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
        real_key_id = base64.b64encode(hashlib.sha256(point).digest()).decode()
        stored = await raw.everything()
        assert f"'attest_key_id': '{real_key_id}'" in stored
        assert f"'attest_public_key': '{base64.b64encode(point).decode()}'" in stored
        assert "'attest_counter': '0'" in stored
        assert f"t:attk:{real_key_id}={account['id']}" in stored


async def test_with_the_check_on_an_account_needs_an_attestation(attested: Attested) -> None:
    async with attested() as (running, _):
        refused(await running.http.post("/v1/accounts"), "attestation_required")
        refused(await create(running.http, None), "attestation_required")


@pytest.mark.parametrize(
    ("change", "step"),
    [
        ({"app_id": "ABCDE12345.com.example.other"}, "the RP ID is not one of the app IDs"),
        ({"counter": 1}, "the counter is not 0"),
        ({"environment": "development"}, "the aaguid is not the one of the production environment"),
        ({"nonce_challenge": "a-different-challenge"}, "the nonce is not the one of this challenge"),
        ({"sent_key_id": bytes(32)}, "the key ID is not the hash of the public key"),
        ({"credential_id": bytes(32)}, "the credential ID is not the key ID"),
        ({"valid_for": timedelta(minutes=30)}, "the certificate chain is not valid"),
        ({"with_intermediate": False}, "x5c must have the credential certificate and the intermediate"),
        ({"fmt": "packed"}, "the format is not apple-appattest"),
        ({"category": 0}, "the validation category 0 is not accepted"),
        ({"category": 7}, "the validation category 7 is not accepted"),
    ],
)
async def test_an_attestation_that_fails_one_step_of_the_procedure_is_refused(
    attested: Attested, caplog: pytest.LogCaptureFixture, change: Json, step: str
) -> None:
    caplog.set_level(logging.WARNING, logger="mcp_client_server.services.integrity")
    async with attested() as (running, apple):
        refused(await create(running.http, apple.attest(Attest(await challenge(running.http), **change))))
        assert step in caplog.records[-1].getMessage()
        # The refused attestation changed nothing: a good one makes the first account.
        good = await create(running.http, apple.attest(Attest(await challenge(running.http))))
        assert (good.status_code, good.json()["account"]["id"]) == (201, "acc_000001")


async def test_an_attestation_from_a_service_that_is_not_trusted_is_refused(
    attested: Attested, clock: FakeClock
) -> None:
    async with attested() as (running, _):
        impostor = FakeAppAttest(clock.time)
        refused(await create(running.http, impostor.attest(Attest(await challenge(running.http)))))


async def test_an_object_that_is_not_an_attestation_is_refused_or_not_valid(attested: Attested) -> None:
    async with attested() as (running, apple):
        http = running.http
        good = apple.attest(Attest(await challenge(http)))
        garbage = base64.b64encode(b"\xff\x00not cbor").decode()
        refused(await create(http, {**good, "object": garbage}))

        shapes: list[tuple[Json, str]] = [
            ({**good, "key_id": "not base64!"}, "attestation.key_id"),
            ({**good, "key_id": base64.b64encode(b"short").decode()}, "attestation.key_id"),
            ({**good, "object": "%%%"}, "attestation.object"),
            ({**good, "challenge": ""}, "attestation.challenge"),
            ({"key_id": good["key_id"], "challenge": good["challenge"]}, "attestation.object"),
            ({**good, "extra": 1}, "attestation.extra"),
        ]
        for body, field in shapes:
            problem = error(await create(http, body), 422, "validation_failed")
            assert list(problem["fields"]) == [field]


async def test_a_challenge_works_one_time_and_for_five_minutes(attested: Attested) -> None:
    async with attested() as (running, apple):
        http, clock = running.http, running.clock

        used = await challenge(http)
        assert (await create(http, apple.attest(Attest(used)))).status_code == 201
        refused(await create(http, apple.attest(Attest(used))))

        refused(await create(http, apple.attest(Attest("a-challenge-of-the-client"))))

        # A failed attempt uses the challenge too.
        burned = await challenge(http)
        refused(await create(http, apple.attest(Attest(burned, counter=1))))
        refused(await create(http, apple.attest(Attest(burned))))

        late = await challenge(http)
        in_time = await challenge(http)
        clock.advance(299)
        assert (await create(http, apple.attest(Attest(in_time)))).status_code == 201
        clock.advance(2)
        refused(await create(http, apple.attest(Attest(late))))


async def test_one_attested_key_makes_one_account_until_that_account_is_deleted(
    attested: Attested, raw: RawStore
) -> None:
    async with attested() as (running, apple):
        http = running.http
        first = Attest(await challenge(http))
        created = await create(http, apple.attest(first))
        assert created.status_code == 201

        again = Attest(await challenge(http), key=first.key)
        refused(await create(http, apple.attest(again)))

        await http.delete("/v1/account", headers=bearer(created.json()["token"]))
        assert [name for name in await raw.keys() if ":rl:" not in name] == []
        assert (await create(http, apple.attest(Attest(await challenge(http), key=first.key)))).status_code == 201


async def test_each_configured_app_id_and_only_the_configured_environment_is_accepted(attested: Attested) -> None:
    async with attested() as (running, apple):
        second = Attest(await challenge(running.http), app_id="ABCDE12345.com.example.second")
        assert (await create(running.http, apple.attest(second))).status_code == 201
    async with attested(app_attest_environment="development") as (running, apple):
        refused(await create(running.http, apple.attest(Attest(await challenge(running.http)))))
        development = Attest(await challenge(running.http), environment="development", category=3)
        assert (await create(running.http, apple.attest(development))).status_code == 201


async def test_with_the_check_off_an_account_needs_no_attestation_and_the_app_can_still_send_one(
    running: Running, clock: FakeClock
) -> None:
    http = running.http
    assert (await http.post("/v1/accounts")).status_code == 201
    assert (await create(http, None)).status_code == 201
    unchecked = FakeAppAttest(clock.time).attest(Attest(await challenge(http), counter=5))
    assert (await create(http, unchecked)).status_code == 201


SAMPLE_TIME = datetime.fromisoformat(SAMPLE["valid_at"])


def verify_sample(
    *,
    client_data_hash: bytes = SAMPLE["client_data_hash_text"].encode(),
    app_id: str = SAMPLE["app_id"],
    environment: appattest.Environment = "production",
    now: datetime = SAMPLE_TIME,
) -> appattest.Attestation:
    return verify_attestation(
        attestation=base64.b64decode(SAMPLE["attestation_object"]),
        key_id=base64.b64decode(SAMPLE["key_id"]),
        client_data_hash=client_data_hash,
        app_ids=[app_id],
        environment=environment,
        roots=appattest.apple_roots(),
        now=now,
    )


def test_apples_sample_attestation_passes_with_apples_root_certificate() -> None:
    # The sample of Apple's "Attestation Object Validation Guide". The sample app gave the challenge text itself as
    # the client data hash. The credential certificate of the sample was valid for three days in April 2026.
    verified = verify_sample()
    assert (verified.counter, len(verified.public_key)) == (0, 65)
    assert hashlib.sha256(verified.public_key).digest() == base64.b64decode(SAMPLE["key_id"])

    with pytest.raises(AttestationError, match="certificate chain"):
        verify_sample(now=datetime(2026, 5, 1, tzinfo=UTC))
    with pytest.raises(AttestationError, match="nonce"):
        verify_sample(client_data_hash=hashlib.sha256(b"example_server_challenge").digest())
    with pytest.raises(AttestationError, match="RP ID"):
        verify_sample(app_id="1234567890.com.example.other")
    with pytest.raises(AttestationError, match="aaguid"):
        verify_sample(environment="development")


@pytest.mark.parametrize(
    ("at", "code"),
    [
        (59, "46119246"),
        (1111111109, "68084774"),
        (1111111111, "67062674"),
        (1234567890, "91819424"),
        (2000000000, "90698825"),
        (20000000000, "77737706"),
    ],
)
def test_codes_are_those_of_rfc_6238_for_sha_256(at: int, code: str) -> None:
    assert totp.code_at(RFC_SEED, at) == code
    assert totp.is_valid(RFC_SEED, code, at)


@pytest.mark.parametrize(
    ("seconds_after_the_step_starts", "accepted"),
    [(-30.5, False), (-30, True), (-0.5, True), (0, True), (29.5, True), (30, True), (59.5, True), (60, False)],
)
async def test_a_code_is_accepted_in_its_step_and_one_step_before_and_after(
    launch: Launch, seconds_after_the_step_starts: float, accepted: bool
) -> None:
    async with launch(app_totp_secret=BASE32_SEED) as running:
        code = totp.code_at(KEY, STEP_START)
        running.clock.tick = 0.0
        running.clock.time = STEP_START + seconds_after_the_step_starts
        response = await running.http.post("/v1/accounts", headers={"X-App-Code": code})
        assert response.status_code == (201 if accepted else 401), response.text


@pytest.mark.parametrize(
    "code", [None, "", "1234567", "abcdefgh", "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18", "00000000"]
)
async def test_a_missing_or_wrong_code_is_refused(launch: Launch, code: str | None) -> None:
    async with launch(app_totp_secret=BASE32_SEED) as running:
        good = totp.code_at(KEY, running.clock.time)
        # A header value goes over the wire as bytes, so the test gives the bytes of the text.
        headers: dict[bytes, bytes] = {} if code is None else {b"X-App-Code": code.encode()}
        for sent in (headers, {b"X-App-Code": (good + "0").encode()}):
            response = await running.http.post("/v1/accounts", headers=sent)
            body = error(response, 401, "app_code_invalid")
            assert body == {"code": "app_code_invalid", "message": "Missing or wrong app code."}
            assert response.headers["www-authenticate"] == "Bearer"


async def test_requests_of_the_app_need_the_code_and_requests_with_an_api_key_do_not(launch: Launch) -> None:
    async with launch(app_totp_secret=BASE32_SEED.lower()) as running:
        http = running.http
        code = {"X-App-Code": totp.code_at(KEY, running.clock.time)}
        api = Api(
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=running.app), base_url="http://t", headers=code)
        )
        account, key, _ = await api.agent()
        task = await api.task(key)
        await api.http.aclose()

        app_routes: list[tuple[str, str, Json | None]] = [
            ("POST", "/v1/attest/challenge", None),
            ("POST", "/v1/accounts", {}),
        ]
        account_routes: list[tuple[str, str, Json | None]] = [
            ("GET", "/v1/account", None),
            ("GET", "/v1/keys", None),
            ("GET", "/v1/tasks", None),
            ("GET", "/v1/questions", None),
            ("PATCH", f"/v1/tasks/{task['id']}", {"progress": 5}),
            ("GET", "/v1/events", None),
        ]
        for method, path, body in app_routes:
            error(await http.request(method, path, json=body), 401, "app_code_invalid")
            assert (await http.request(method, path, json=body, headers=code)).status_code in (200, 201)
        for method, path, body in account_routes:
            error(await http.request(method, path, json=body, headers=account), 401, "app_code_invalid")
            if path != "/v1/events":
                with_code = await http.request(method, path, json=body, headers={**account, **code})
                assert with_code.status_code == 200, path

        # An API key is not the app: no code, also on a route that both credential types can use.
        assert (await http.get("/v1/tasks", headers=key)).status_code == 200
        assert (await http.post("/v1/questions", json={"prompt": "Go?"}, headers=key)).status_code == 201
        assert (await running.api.rpc(key, "tools/list")).status_code == 200
        error(await http.get("/v1/keys", headers=key), 403, "forbidden")
        error(await http.get("/v1/account"), 401, "unauthorized")


def test_the_secret_is_base32_and_a_bad_secret_or_app_id_stops_the_startup() -> None:
    spaced = TestSettings.model_validate({"app_totp_secret": "jbsw y3dp ehpk 3pxp"})
    assert spaced.totp_key == totp.decode_secret("JBSWY3DPEHPK3PXP")
    assert TestSettings().totp_key is None
    assert TestSettings(app_attest_app_ids=" A1.com.x , B2.org.y ").attest_app_ids == ("A1.com.x", "B2.org.y")
    for bad in ({"app_totp_secret": "not base32!"}, {"app_attest_app_ids": "nodots"}, {"app_attest_environment": "x"}):
        with pytest.raises(ValidationError):
            TestSettings.model_validate(bad)

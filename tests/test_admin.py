"""The operator command that deletes an account for a support request."""

import io
from collections.abc import Sequence

import pytest

from mcp_client_server import admin, cli
from mcp_client_server.config import Settings
from mcp_client_server.main import Runtime
from tests.support import DEVICE_A, RawStore, Running, secret_of


async def test_delete_account_by_key_id_removes_everything_and_prints_only_ids(running: Running, raw: RawStore) -> None:
    api = running.api
    account, key, info = await api.agent()
    other_account, other_key, _ = await api.agent("Other")
    await api.device(account, DEVICE_A)
    task = await api.task(key)
    await api.ask(key, task_id=task["id"])
    account_id = await api.account_id(account)
    out = io.StringIO()

    code = await admin.delete_account(running.services, key_id=info["id"], out=out)

    assert code == admin.OK
    assert out.getvalue() == (
        f"Deleted the account {account_id} with its devices, keys, tasks, questions, and inbox items.\n"
    )
    assert secret_of(account) not in out.getvalue()
    assert secret_of(key) not in out.getvalue()
    assert (await api.http.get("/v1/account", headers=account)).status_code == 401
    assert (await api.http.get("/v1/tasks", headers=key)).status_code == 401
    assert [name for name in await raw.keys() if account_id in name or info["id"] in name or task["id"] in name] == []
    # The other account is not touched.
    assert (await api.http.get("/v1/account", headers=other_account)).status_code == 200
    assert (await api.http.get("/v1/tasks", headers=other_key)).status_code == 200


async def test_delete_account_by_account_id_and_an_unknown_target(running: Running) -> None:
    api = running.api
    account = await api.account()
    account_id = await api.account_id(account)
    out = io.StringIO()

    assert await admin.delete_account(running.services, account_id=account_id, out=out) == admin.OK
    assert (await api.http.get("/v1/account", headers=account)).status_code == 401
    assert await admin.delete_account(running.services, account_id=account_id, out=out) == admin.NOT_FOUND
    assert await admin.delete_account(running.services, key_id="key_missing", out=out) == admin.NOT_FOUND
    assert out.getvalue().splitlines()[1:] == [f"No account {account_id}.", "No account has the key key_missing."]
    with pytest.raises(ValueError, match="account ID or a key ID"):
        await admin.delete_account(running.services, out=out)


def test_the_command_line_names_the_account_or_the_key_and_nothing_else() -> None:
    by_key = cli.parse_args(["admin", "delete-account", "--key-id", "key_1"])
    assert (by_key.command, by_key.admin_command, by_key.key_id, by_key.account_id) == (
        "admin",
        "delete-account",
        "key_1",
        None,
    )
    assert cli.parse_args(["admin", "delete-account", "--account-id", "acc_1"]).account_id == "acc_1"
    assert cli.parse_args([]).command is None
    for wrong in (
        ["admin"],
        ["admin", "delete-account"],
        ["admin", "delete-account", "--key-id", "k", "--account-id", "a"],
    ):
        with pytest.raises(SystemExit) as refused:
            cli.parse_args(wrong)
        assert refused.value.code == 2


def test_main_exits_with_the_code_of_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Sequence[str | None]] = []

    async def fake_run_admin(args: object) -> int:
        seen.append((getattr(args, "key_id", None), getattr(args, "account_id", None)))
        return admin.NOT_FOUND

    monkeypatch.setattr(cli, "run_admin", fake_run_admin)
    with pytest.raises(SystemExit) as finished:
        cli.main(["admin", "delete-account", "--key-id", "key_1"])
    assert finished.value.code == admin.NOT_FOUND
    assert seen == [("key_1", None)]


async def test_run_admin_deletes_through_the_runtime_and_closes_it(
    running: Running, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = running.api
    account, _, info = await api.agent()
    closed: list[bool] = []

    async def close() -> None:
        closed.append(True)

    def build(_: Settings) -> Runtime:
        return Runtime(running.services, (close,))

    monkeypatch.setattr(cli, "build_runtime", build)
    code = await cli.run_admin(cli.parse_args(["admin", "delete-account", "--key-id", info["id"]]))

    assert code == admin.OK
    assert closed == [True]
    assert "Deleted the account" in capsys.readouterr().out
    assert (await api.http.get("/v1/account", headers=account)).status_code == 401

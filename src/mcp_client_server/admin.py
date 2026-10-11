"""Operator commands. Each one runs on the service layer and prints no secret."""

from typing import TextIO

from mcp_client_server.credentials import AccountPrincipal
from mcp_client_server.ids import AccountId, KeyId
from mcp_client_server.services import Services

OK = 0
NOT_FOUND = 1


async def delete_account(
    services: Services, *, account_id: str | None = None, key_id: str | None = None, out: TextIO
) -> int:
    """Delete one account with all its data, named by its ID or by the ID of one of its keys. Returns the exit code."""
    if key_id is not None:
        found = await services.accounts.account_of_key(KeyId(key_id))
        if found is None:
            out.write(f"No account has the key {key_id}.\n")
            return NOT_FOUND
        account_id = found
    if account_id is None:
        raise ValueError("Give an account ID or a key ID.")
    if not await services.accounts.delete(AccountPrincipal(AccountId(account_id))):
        out.write(f"No account {account_id}.\n")
        return NOT_FOUND
    out.write(f"Deleted the account {account_id} with its devices, keys, tasks, questions, and inbox items.\n")
    return OK

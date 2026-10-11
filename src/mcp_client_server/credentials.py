import hashlib
import secrets
import string
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NewType

from mcp_client_server.ids import AccountId, KeyId

Secret = NewType("Secret", str)
CredentialHash = NewType("CredentialHash", str)
SecretPrefix = Literal["mca_", "mck_"]

ACCOUNT_TOKEN_PREFIX: SecretPrefix = "mca_"  # noqa: S105  a public prefix, not a secret
API_KEY_PREFIX: SecretPrefix = "mck_"

_ALPHABET = string.ascii_letters + string.digits
# 43 base62 characters carry 256 bits.
_SECRET_LENGTH = 43
_MAX_LENGTH = 128


@dataclass(frozen=True, slots=True)
class AccountPrincipal:
    account_id: AccountId


@dataclass(frozen=True, slots=True)
class AgentPrincipal:
    account_id: AccountId
    key_id: KeyId


type Principal = AccountPrincipal | AgentPrincipal


def new_secret(prefix: SecretPrefix) -> Secret:
    return Secret(prefix + "".join(secrets.choice(_ALPHABET) for _ in range(_SECRET_LENGTH)))


def hash_secret(secret: Secret) -> CredentialHash:
    return CredentialHash(hashlib.sha256(secret.encode()).hexdigest())


def looks_like_credential(value: str) -> bool:
    return value.startswith((ACCOUNT_TOKEN_PREFIX, API_KEY_PREFIX)) and len(value) <= _MAX_LENGTH


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def presented_secret(headers: Mapping[str, str], query: Mapping[str, str] | None = None) -> Secret | None:
    """The credential of a request. `query` is given only where `?key=` is accepted."""
    token = bearer_token(headers.get("authorization"))
    if token:
        return Secret(token)
    api_key = headers.get("x-api-key") or (query.get("key") if query is not None else None)
    return Secret(api_key) if api_key and api_key.startswith(API_KEY_PREFIX) else None

import hashlib
import secrets
import string
from dataclasses import dataclass

ACCOUNT_TOKEN_PREFIX = "mca_"
API_KEY_PREFIX = "mck_"

_ALPHABET = string.ascii_letters + string.digits
# 43 base62 characters carry 256 bits.
_SECRET_LENGTH = 43
_MAX_LENGTH = 128


@dataclass(frozen=True, slots=True)
class Principal:
    account_id: str
    key_id: str | None = None

    @property
    def is_agent(self) -> bool:
        return self.key_id is not None


def new_secret(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(_SECRET_LENGTH))


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def looks_like_credential(value: str) -> bool:
    return value.startswith((ACCOUNT_TOKEN_PREFIX, API_KEY_PREFIX)) and len(value) <= _MAX_LENGTH


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None

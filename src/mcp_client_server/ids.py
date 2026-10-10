import secrets
import time

_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def now() -> float:
    return round(time.time(), 6)


def new_id(prefix: str) -> str:
    """Prefix plus a lowercase ULID, so IDs made later sort later."""
    value = ((time.time_ns() // 1_000_000) << 80) | secrets.randbits(80)
    body = "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))
    return f"{prefix}_{body}"

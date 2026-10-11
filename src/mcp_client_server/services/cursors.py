"""The opaque cursor of a list that is in the order of a time, newest first."""

import base64
import math

from mcp_client_server.errors import ValidationFailed


def encode(score: float, item_id: str) -> str:
    return base64.urlsafe_b64encode(f"{score!r}:{item_id}".encode()).decode().rstrip("=")


def decode(value: str | None) -> tuple[float, str] | None:
    """The time and the ID of the last item of the previous page. None: start from the top."""
    if not value:
        return None
    problem = ValidationFailed({"cursor": "Not a valid cursor. Use the next_cursor of the previous page."})
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        raw_score, _, item_id = text.partition(":")
        score = float(raw_score)
    except ValueError as exc:
        raise problem from exc
    # The store cannot compare with a score that is not a finite number.
    if not item_id or not math.isfinite(score):
        raise problem
    return score, item_id

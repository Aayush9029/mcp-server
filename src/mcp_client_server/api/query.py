from mcp_client_server.errors import ValidationFailed
from mcp_client_server.models import split_commas


def choices[T: str](name: str, raw: str | None, allowed: tuple[T, ...]) -> list[T]:
    """The values of a query parameter that has one value or several with commas between them."""
    if raw is None:
        return []
    wanted = set(split_commas(raw))
    if not wanted <= set(allowed):
        raise ValidationFailed({name: f"Use one or more of: {', '.join(allowed)}."})
    return [value for value in allowed if value in wanted]

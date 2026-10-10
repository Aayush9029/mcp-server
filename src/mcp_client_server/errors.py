class AppError(Exception):
    status = 500
    code = "internal"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Unauthorized(AppError):
    status = 401
    code = "unauthorized"


class Forbidden(AppError):
    status = 403
    code = "forbidden"


class NotFound(AppError):
    status = 404
    code = "not_found"


class Conflict(AppError):
    status = 409
    code = "conflict"


class LimitReached(Conflict):
    """A count limit from docs/api.md is full."""


class ValidationFailed(AppError):
    status = 422
    code = "validation_failed"

    def __init__(self, fields: dict[str, str], message: str = "The request is not valid.") -> None:
        super().__init__(message)
        self.fields = fields


class RateLimited(AppError):
    status = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)

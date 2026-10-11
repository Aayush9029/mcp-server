from typing import ClassVar, Literal

ErrorCode = Literal[
    "bad_request",
    "unauthorized",
    "forbidden",
    "not_found",
    "method_not_allowed",
    "conflict",
    "validation_failed",
    "rate_limited",
    "internal",
    "unavailable",
    "attestation_required",
    "attestation_invalid",
    "app_code_invalid",
]


class AppError(Exception):
    status: ClassVar[int] = 500
    code: ClassVar[ErrorCode] = "internal"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Unauthorized(AppError):
    status = 401
    code = "unauthorized"


class AttestationRequired(Unauthorized):
    code = "attestation_required"


class AttestationInvalid(Unauthorized):
    code = "attestation_invalid"


class AppCodeInvalid(Unauthorized):
    code = "app_code_invalid"


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
    pass


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


class StoreUnavailable(AppError):
    status = 503
    code = "unavailable"
    retry_after: ClassVar[int] = 2

    def __init__(self, message: str = "The service is not available now. Try again in a few seconds.") -> None:
        super().__init__(message)

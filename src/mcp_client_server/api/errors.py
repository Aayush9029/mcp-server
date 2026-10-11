import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter
from starlette.exceptions import HTTPException

from mcp_client_server import activity
from mcp_client_server.errors import AppError, ErrorCode, RateLimited, StoreUnavailable, Unauthorized, ValidationFailed
from mcp_client_server.models import ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)

_SERVER_ERROR = 500
_HTTP_CODES: dict[int, ErrorCode] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_failed",
    429: "rate_limited",
    503: "unavailable",
}
_HTTP_MESSAGES = {404: "Not found.", 405: "Method not allowed."}
_REQUEST_PARTS = {"body", "query", "path", "header", "cookie"}


def code_of(status: int) -> ErrorCode:
    return _HTTP_CODES.get(status, "bad_request" if status < _SERVER_ERROR else "internal")


def error_response(
    status: int,
    code: ErrorCode,
    message: str,
    *,
    fields: dict[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """The error body of the contract. The activity log of the request gets the same code."""
    activity.failed(code, message, fields)
    body = ErrorResponse(error=ErrorBody(code=code, message=message, fields=fields))
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status, headers=headers)


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):
        raise exc
    headers: dict[str, str] = {}
    if isinstance(exc, (RateLimited, StoreUnavailable)):
        headers["Retry-After"] = str(exc.retry_after)
    if isinstance(exc, Unauthorized):
        headers["WWW-Authenticate"] = "Bearer"
    if isinstance(exc, StoreUnavailable):
        logger.warning("the store does not answer (%s)", type(exc.__cause__ or exc).__name__)
    fields = exc.fields if isinstance(exc, ValidationFailed) else None
    return error_response(exc.status, exc.code, exc.message, fields=fields, headers=headers)


class _Problem(BaseModel):
    type: str
    loc: tuple[int | str, ...]
    msg: str

    @property
    def field(self) -> str:
        parts = [str(part) for part in self.loc]
        if len(parts) > 1 and parts[0] in _REQUEST_PARTS:
            parts = parts[1:]
        return ".".join(parts)


_PROBLEMS = TypeAdapter(list[_Problem])


async def _request_validation(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    problems = _PROBLEMS.validate_python(exc.errors())
    if any(problem.type == "json_invalid" for problem in problems):
        return error_response(400, "bad_request", "The body is not valid JSON.")
    fields = {problem.field: problem.msg.removeprefix("Value error, ") for problem in problems}
    return error_response(422, "validation_failed", "The request is not valid.", fields=fields)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise exc
    code = code_of(exc.status_code)
    message = _HTTP_MESSAGES.get(exc.status_code, str(exc.detail))
    return error_response(exc.status_code, code, message, headers=exc.headers)


async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled error: %s", type(exc).__name__)
    return error_response(500, "internal", "Internal error.")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)

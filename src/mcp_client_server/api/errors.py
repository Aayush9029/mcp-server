import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from mcp_client_server.errors import AppError, RateLimited, Unauthorized, ValidationFailed

logger = logging.getLogger(__name__)

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_failed",
    429: "rate_limited",
}
_HTTP_MESSAGES = {404: "Not found.", 405: "Method not allowed."}
_REQUEST_PARTS = {"body", "query", "path", "header", "cookie"}


def error_response(
    status: int, code: str, message: str, *, fields: dict[str, str] | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if fields is not None:
        error["fields"] = fields
    return JSONResponse({"error": error}, status_code=status, headers=headers)


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimited):
        headers["Retry-After"] = str(exc.retry_after)
    if isinstance(exc, Unauthorized):
        headers["WWW-Authenticate"] = "Bearer"
    fields = exc.fields if isinstance(exc, ValidationFailed) else None
    return error_response(exc.status, exc.code, exc.message, fields=fields, headers=headers)


def _field_name(loc: tuple[Any, ...]) -> str:
    parts = [str(part) for part in loc]
    if len(parts) > 1 and parts[0] in _REQUEST_PARTS:
        parts = parts[1:]
    return ".".join(parts)


async def _request_validation(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    if any(error["type"] == "json_invalid" for error in errors):
        return error_response(400, "bad_request", "The body is not valid JSON.")
    fields = {_field_name(error["loc"]): str(error["msg"]).removeprefix("Value error, ") for error in errors}
    return error_response(422, "validation_failed", "The request is not valid.", fields=fields)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    code = _HTTP_CODES.get(exc.status_code, "bad_request" if exc.status_code < 500 else "internal")
    message = _HTTP_MESSAGES.get(exc.status_code, str(exc.detail))
    return error_response(exc.status_code, code, message, headers=dict(exc.headers or {}))


async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled error: %s", type(exc).__name__)
    return error_response(500, "internal", "Internal error.")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)

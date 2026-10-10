from typing import Annotated

from fastapi import Depends, Request

from mcp_client_server.credentials import API_KEY_PREFIX, Principal, bearer_token
from mcp_client_server.errors import Forbidden
from mcp_client_server.services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


def request_credential(request: Request, *, allow_query: bool = False) -> str | None:
    token = bearer_token(request.headers.get("authorization"))
    if token:
        return token
    api_key = request.headers.get("x-api-key") or (request.query_params.get("key") if allow_query else None)
    return api_key if api_key and api_key.startswith(API_KEY_PREFIX) else None


async def any_principal(request: Request, services: ServicesDep) -> Principal:
    return await services.auth.authenticate(request_credential(request))


async def account_principal(principal: Annotated[Principal, Depends(any_principal)]) -> Principal:
    if principal.is_agent:
        raise Forbidden("This route needs the account token. An API key cannot use it.")
    return principal


AnyPrincipal = Annotated[Principal, Depends(any_principal)]
AccountPrincipal = Annotated[Principal, Depends(account_principal)]

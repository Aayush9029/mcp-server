from typing import Annotated

from fastapi import Depends, Request

from mcp_client_server.credentials import (
    ACCOUNT_TOKEN_PREFIX,
    AccountPrincipal,
    AgentPrincipal,
    Principal,
    presented_secret,
)
from mcp_client_server.errors import Forbidden
from mcp_client_server.services import Services


def get_services(request: Request) -> Services:
    services: object = request.app.state.services
    if not isinstance(services, Services):
        raise TypeError("The application has no services.")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def app_code(request: Request, services: ServicesDep) -> None:
    """For a route of the app that has no credential. Does nothing if the app code check is off."""
    services.integrity.check_app_code(request.headers.get("x-app-code"))


async def any_principal(request: Request, services: ServicesDep) -> Principal:
    secret = presented_secret(request.headers)
    # Only the app has an account token, so only such a request needs the app code. An API key does not.
    if secret is not None and secret.startswith(ACCOUNT_TOKEN_PREFIX):
        services.integrity.check_app_code(request.headers.get("x-app-code"))
    return await services.auth.authenticate(secret)


async def account_principal(principal: Annotated[Principal, Depends(any_principal)]) -> AccountPrincipal:
    if not isinstance(principal, AccountPrincipal):
        raise Forbidden("This route needs the account token. An API key cannot use it.")
    return principal


async def agent_principal(principal: Annotated[Principal, Depends(any_principal)]) -> AgentPrincipal:
    if not isinstance(principal, AgentPrincipal):
        raise Forbidden("This route needs an API key. The account token cannot use it.")
    return principal


AppCode = Depends(app_code)
AnyAuth = Annotated[Principal, Depends(any_principal)]
AgentAuth = Annotated[AgentPrincipal, Depends(agent_principal)]
AccountAuth = Annotated[AccountPrincipal, Depends(account_principal)]

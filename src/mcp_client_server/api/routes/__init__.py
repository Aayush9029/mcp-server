from fastapi import APIRouter

from mcp_client_server.api.routes import accounts, system, tasks

router = APIRouter()
router.include_router(system.router)
router.include_router(accounts.router)
router.include_router(tasks.router)

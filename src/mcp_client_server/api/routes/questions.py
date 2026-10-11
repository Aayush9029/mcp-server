from typing import Annotated

from fastapi import APIRouter, Query, Response

from mcp_client_server import limits
from mcp_client_server.api.deps import AccountAuth, AgentAuth, AnyAuth, ServicesDep
from mcp_client_server.api.query import choices
from mcp_client_server.ids import QuestionId
from mcp_client_server.models import QUESTION_STATUSES, Question, QuestionAnswer, QuestionCreate

router = APIRouter(prefix="/v1/questions")


@router.get("", response_model=None)
async def list_questions(principal: AnyAuth, services: ServicesDep, status: str | None = None) -> Response:
    page = await services.questions.page(principal, choices("status", status, QUESTION_STATUSES))
    return Response(page.to_json(), media_type="application/json")


@router.post("", status_code=201)
async def ask_question(data: QuestionCreate, principal: AgentAuth, services: ServicesDep) -> Question:
    return await services.questions.ask(principal, data)


@router.get("/{question_id}")
async def get_question(
    question_id: QuestionId,
    principal: AnyAuth,
    services: ServicesDep,
    wait: Annotated[int, Query(ge=0, le=limits.WAIT_MAX_SECONDS)] = 0,
) -> Question:
    return await services.questions.get(principal, question_id, wait=wait)


@router.post("/{question_id}/answer")
async def answer_question(
    question_id: QuestionId, data: QuestionAnswer, principal: AccountAuth, services: ServicesDep
) -> Question:
    return await services.questions.answer(principal, question_id, data)


@router.delete("/{question_id}", status_code=204, response_class=Response)
async def cancel_question(question_id: QuestionId, principal: AgentAuth, services: ServicesDep) -> None:
    await services.questions.cancel(principal, question_id)

"""The pure rules of a task: patch semantics, cursors, and the one-line fields."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from mcp_client_server.errors import ValidationFailed
from mcp_client_server.ids import TaskId
from mcp_client_server.models import PRIORITIES, STATUSES, Label, PatchField, Task, TaskPatch, Title
from mcp_client_server.services.tasks import apply_patch, decode_cursor, encode_cursor
from mcp_client_server.store import Cursor
from tests.support import make_task

PATCH_FIELDS: tuple[PatchField, ...] = (
    "title",
    "description",
    "status",
    "priority",
    "notify",
    "progress",
    "status_note",
)
progress_values = st.none() | st.integers(0, 100)
note_values = st.none() | st.text("ab ", max_size=6)


@st.composite
def tasks(draw: st.DrawFn) -> Task:
    status = draw(st.sampled_from(STATUSES))
    return make_task(
        title=draw(st.sampled_from(["One", "Two"])),
        description=draw(st.sampled_from(["", "Text"])),
        status=status,
        priority=draw(st.sampled_from(PRIORITIES)),
        notify=draw(st.booleans()),
        progress=100 if status == "done" else draw(progress_values),
        status_note=draw(st.none() | st.sampled_from(["a", "a b"])),
        updated_at=draw(st.floats(1e9, 2e9)),
        completed_at=1.5e9 if status == "done" else None,
    )


@st.composite
def patches(draw: st.DrawFn) -> TaskPatch:
    values: dict[PatchField, object] = {
        "title": draw(st.sampled_from(["One", "Two", "Three"])),
        "description": draw(st.sampled_from(["", "Text", "More"])),
        "status": draw(st.sampled_from(STATUSES)),
        "priority": draw(st.sampled_from(PRIORITIES)),
        "notify": draw(st.booleans()),
        "progress": draw(progress_values),
        "status_note": draw(note_values),
    }
    sent = draw(st.sets(st.sampled_from(PATCH_FIELDS)))
    return TaskPatch.model_validate({name: values[name] for name in sent})


def values_of(task: Task) -> dict[PatchField, object]:
    return {
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "notify": task.notify,
        "progress": task.progress,
        "status_note": task.status_note,
    }


@given(task=tasks(), patch=patches(), at=st.floats(1e9, 2e9))
def test_patch_changes_exactly_the_fields_that_it_reports(task: Task, patch: TaskPatch, at: float) -> None:
    patched, changed = apply_patch(task, patch, at)
    before, after = values_of(task), values_of(patched)

    assert changed == {name for name in PATCH_FIELDS if before[name] != after[name]}
    # A field that the client did not send stays, except that `done` owns the progress.
    for name in set(PATCH_FIELDS) - patch.model_fields_set:
        if not (name == "progress" and patched.status == "done"):
            assert after[name] == before[name]
    assert (patched.id, patched.key_id, patched.source, patched.created_at) == (
        task.id,
        task.key_id,
        task.source,
        task.created_at,
    )


@given(task=tasks(), patch=patches(), at=st.floats(1e9, 2e9))
def test_patch_keeps_the_invariants_of_a_task(task: Task, patch: TaskPatch, at: float) -> None:
    patched, changed = apply_patch(task, patch, at)

    assert (patched.status == "done") == (patched.completed_at is not None)
    if patched.status == "done":
        assert patched.progress == 100
    assert patched.status_note != ""
    if changed:
        assert patched.updated_at > task.updated_at
        assert patched.updated_at >= at
    else:
        assert patched is task


@given(task=tasks(), patch=patches(), at=st.floats(1e9, 2e9))
def test_the_same_patch_a_second_time_changes_nothing(task: Task, patch: TaskPatch, at: float) -> None:
    patched, _ = apply_patch(task, patch, at)
    again, changed = apply_patch(patched, patch, at + 60)
    assert again is patched
    assert changed == frozenset()


def test_done_sets_the_progress_and_the_completion_time() -> None:
    patched, changed = apply_patch(make_task(), TaskPatch(status="done", progress=50), 2e9)
    assert (patched.status, patched.progress, patched.completed_at, patched.updated_at) == ("done", 100, 2e9, 2e9)
    assert changed == {"status", "progress"}


def test_a_done_task_keeps_its_completion_time_until_it_is_opened_again() -> None:
    done = make_task(status="done", progress=100, completed_at=1.7e9)
    renamed, _ = apply_patch(done, TaskPatch(title="New"), 2e9)
    assert renamed.completed_at == 1.7e9
    reopened, changed = apply_patch(done, TaskPatch(status="in_progress"), 2e9)
    assert (reopened.completed_at, reopened.progress, changed) == (None, 100, {"status"})


def test_null_and_empty_text_clear_the_optional_fields() -> None:
    patched, changed = apply_patch(make_task(), TaskPatch(progress=None, status_note=""), 2e9)
    assert (patched.progress, patched.status_note) == (None, None)
    assert changed == {"progress", "status_note"}


def test_two_writes_in_one_microsecond_still_sort_in_write_order() -> None:
    patched, _ = apply_patch(make_task(updated_at=500.0), TaskPatch(title="New"), 400.0)
    assert patched.updated_at == 500.000001


@given(score=st.floats(allow_nan=False, allow_infinity=False), task_id=st.text(min_size=1).map(TaskId))
def test_cursor_round_trip(score: float, task_id: TaskId) -> None:
    cursor = Cursor(score, task_id)
    assert decode_cursor(encode_cursor(cursor)) == cursor


@given(text=st.text())
def test_any_text_is_a_cursor_or_a_validation_error(text: str) -> None:
    try:
        cursor = decode_cursor(text)
    except ValidationFailed:
        return
    assert cursor is None or (math.isfinite(cursor.score) and cursor.task_id)


@pytest.mark.parametrize("text", ["bmFuOnRza18x", "aW5mOnRza18x", "MS4wOg", "dGVzdA", "!!"])
def test_cursor_rejects_scores_and_shapes_that_the_store_cannot_use(text: str) -> None:
    # nan:tsk_1, inf:tsk_1, a cursor with no task ID, text with no colon, and text that is not base64.
    with pytest.raises(ValidationFailed) as problem:
        decode_cursor(text)
    assert list(problem.value.fields) == ["cursor"]


@pytest.mark.parametrize("field", [TypeAdapter[str](Title), TypeAdapter[str](Label)])
@given(text=st.text(st.characters(codec="utf-8", categories=["L", "N", "Z", "Cc"]), max_size=60))
def test_one_line_fields_have_no_line_break_and_no_extra_space(field: TypeAdapter[str], text: str) -> None:
    try:
        value = field.validate_python(text)
    except ValidationError:
        return
    assert value == " ".join(value.split())
    assert value
    assert field.validate_python(value) == value

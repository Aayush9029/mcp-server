"""Limits from docs/api.md. Change both places together."""

from dataclasses import dataclass

TITLE_MAX = 200
DESCRIPTION_MAX = 20_000
STATUS_NOTE_MAX = 140
LABEL_MAX = 40
NOTIFY_TITLE_MAX = 100
NOTIFY_BODY_MAX = 1000

PROMPT_MAX = 300
OPTION_MAX = 30
OPTIONS_MIN = 2
OPTIONS_MAX = 4
ANSWER_MAX = 1000
EXPIRES_IN_MIN = 60
EXPIRES_IN_MAX = 24 * 3600
EXPIRES_IN_DEFAULT = 3600
WAIT_MAX_SECONDS = 50
MCP_WAIT_DEFAULT_SECONDS = 30
QUESTION_LIST_MAX = 100

LIST_LIMIT_MAX = 500
LIST_LIMIT_DEFAULT = 100
MCP_LIST_LIMIT_DEFAULT = 50

LAST_USED_WRITE_INTERVAL_SECONDS = 60
LIVE_ACTIVITY_DISMISS_SECONDS = 15 * 60
WIDGET_PUSH_INTERVAL_SECONDS = 15
ATTEST_CHALLENGE_TTL_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits:
    max_keys_per_account: int = 20
    max_devices_per_account: int = 20
    max_open_tasks_per_key: int = 1000
    notify_user_per_hour: int = 30
    closed_task_ttl_seconds: int = 90 * 24 * 3600
    max_pending_questions_per_key: int = 20
    closed_question_ttl_seconds: int = 90 * 24 * 3600
    # A filter on priority reads tasks that it does not return. One request reads this many at most.
    filter_scan_budget: int = 2000

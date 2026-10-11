"""Limits from docs/overview.md. Change both places together."""

from dataclasses import dataclass

# The largest field is a description of 20,000 characters, and a character is 4 bytes at most.
MAX_BODY_BYTES = 1024 * 1024

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

MESSAGE_MAX = 2000
INBOX_LIMIT_MAX = 50
INBOX_LIMIT_DEFAULT = 20
INBOX_VIEW_COUNT = 50

HOST_MAX = 64
CLIENT_FIELD_MAX = 100
ACTIVITY_LIMIT_MAX = 100
ACTIVITY_LIMIT_DEFAULT = 50

LIST_LIMIT_MAX = 500
LIST_LIMIT_DEFAULT = 100
MCP_LIST_LIMIT_DEFAULT = 50

LAST_USED_WRITE_INTERVAL_SECONDS = 60
LIVE_ACTIVITY_DISMISS_SECONDS = 15 * 60
# A Live Activity gets one `update` in this time at most, with the latest state. The hourly budget of the
# system is not published; this keeps a chatty agent under 720 updates in one hour for one task.
LIVE_ACTIVITY_UPDATE_INTERVAL_SECONDS = 5
# The system shows the Live Activity as stale when no update came for this long.
LIVE_ACTIVITY_STALE_SECONDS = 30 * 60
WIDGET_PUSH_INTERVAL_SECONDS = 15
ATTEST_CHALLENGE_TTL_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits:
    max_keys_per_account: int = 20
    max_devices_per_account: int = 20
    max_open_tasks_per_key: int = 1000
    # The oldest closed task of a key is removed when one more closes.
    max_closed_tasks_per_key: int = 5000
    notify_user_per_hour: int = 30
    closed_task_ttl_seconds: int = 90 * 24 * 3600
    max_pending_questions_per_key: int = 20
    max_unread_inbox_items: int = 200
    unread_inbox_ttl_seconds: int = 7 * 24 * 3600
    read_inbox_ttl_seconds: int = 24 * 3600
    messages_per_hour: int = 60
    # The activity log of a key keeps this many requests, and none that is older than the lifetime.
    activity_log_entries: int = 200
    activity_log_ttl_seconds: int = 7 * 24 * 3600
    closed_question_ttl_seconds: int = 90 * 24 * 3600
    # A filter on priority reads tasks that it does not return. One request reads this many at most.
    filter_scan_budget: int = 2000
    # Both are for one server process. A stream above the first ends the oldest stream of the account.
    max_streams_per_account: int = 16
    max_waits_per_key: int = 32
    # A deleted key loses its data in steps of this many items, so no step holds the store for long.
    purge_batch: int = 500

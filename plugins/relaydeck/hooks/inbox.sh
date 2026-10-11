#!/bin/sh
# Relaydeck inbox hook. One script serves the hooks of each coding agent.
#
#   inbox.sh AGENT EVENT [SERVER_NAME]
#
#   AGENT        claude, codex, gemini, or cursor: where the API key is, and the output format
#   EVENT        start   a session starts: unread items go into the context of the agent
#                prompt  the person sends a prompt: unread items go into the context
#                stop    the agent is about to stop: unread items make it continue
#                watch   Claude Code only, in the background: wait for an item, then wake the agent
#   SERVER_NAME  the name of the MCP server in the configuration of the agent (default: tasks)
#
# The script uses sh, curl, and awk, which are the tools of the installer. It does not use python3
# or jq, because no agent brings them. curl gets the API key through stdin, and the script prints
# the key nowhere. When the server does not answer, the script prints nothing and exits 0.
#
# Each request has the header X-Relaydeck-Host with the name of this machine, so the app can show
# where the agent runs. The name comes from the configuration of the agent, else from
# RELAYDECK_HOST, else from the short host name.
#
# Environment, for a self-hosted server and for the tests:
#   RELAYDECK_URL           the server, for example http://localhost:8000
#   RELAYDECK_HOST          the name of this machine for the app
#   RELAYDECK_HOOK_TIMEOUT  seconds for one request (default 5)
#   RELAYDECK_HOOK_WAIT     seconds that "watch" holds one request open (default 50)
#   RELAYDECK_HOOK_PAUSE    seconds that "watch" waits after a request that failed (default 30)

set -u
umask 077

VERSION=1.2.0
DEFAULT_BASE="https://relaydeck.onrender.com"
LIMIT=5
WATCH_MINUTES=480
AGENT=${1:-}
EVENT=${2:-}
SERVER=${3:-tasks}
NL='
'

# Prints one line for each scalar of a JSON text: PATH, TYPE, VALUE, with a tab between them.
# TYPE is "s" for a string and "v" for a number, true, false, or null. A string keeps its JSON
# escapes. The reader also accepts comments and trailing commas, which some agents permit.
# It prints nothing for a text that is cut off or is not JSON.
# shellcheck disable=SC2016  # an awk program: $0 and $1 are awk fields
JSON_FLAT='
function emit(type, value,    i, path) {
    path = ""
    for (i = 1; i <= depth; i++) path = path (i > 1 ? "." : "") name[i]
    gsub(/[\t\r\n]/, " ", value)
    lines = lines path "\t" type "\t" value "\n"
}
{ text = text $0 "\n" }
END {
    depth = 0
    for (;;) {
        if (match(text, /^[ \t\r\n]+/)) text = substr(text, RLENGTH + 1)
        if (text == "") break
        c = substr(text, 1, 1)
        if (c == "\"") {
            if (!match(text, /^"([^"\\]|\\.)*"/)) break
            token = substr(text, 2, RLENGTH - 2)
            text = substr(text, RLENGTH + 1)
            if (depth > 0 && kind[depth] == "o" && key[depth]) {
                name[depth] = token
                key[depth] = 0
            } else emit("s", token)
        } else if (c == "{" || c == "[") {
            depth++
            kind[depth] = (c == "{") ? "o" : "a"
            key[depth] = (c == "{")
            name[depth] = (c == "{") ? "" : 0
            text = substr(text, 2)
        } else if (c == "}" || c == "]") {
            if (depth > 0) depth--
            text = substr(text, 2)
        } else if (c == ",") {
            if (depth > 0 && kind[depth] == "o") key[depth] = 1
            else if (depth > 0) name[depth]++
            text = substr(text, 2)
        } else if (c == ":") {
            text = substr(text, 2)
        } else if (substr(text, 1, 2) == "//") {
            i = index(text, "\n")
            if (!i) break
            text = substr(text, i + 1)
        } else if (substr(text, 1, 2) == "/*") {
            i = index(text, "*/")
            if (!i) break
            text = substr(text, i + 2)
        } else if (match(text, /^(-?[0-9][-+0-9.eE]*|true|false|null)/)) {
            emit("v", substr(text, 1, RLENGTH))
            text = substr(text, RLENGTH + 1)
        } else break
    }
    if (text == "" && depth == 0) printf "%s", lines
}'

# Reads the lines of JSON_FLAT for an inbox answer. Prints the IDs of the items on line 1, then the
# text for the agent. With json=1, the text is the content of a JSON string on one line.
# The text of an item stays a JSON string, so an item can not start a line that looks like a
# different item. "skip" holds the IDs that the agent got last time.
# shellcheck disable=SC2016  # an awk program: $0 and $1 are awk fields
INBOX_TEXT='
function escaped(s,    out) {
    out = ""
    while (match(s, /["\\]/)) {
        out = out substr(s, 1, RSTART - 1) "\\" substr(s, RSTART, 1)
        s = substr(s, RSTART + 1)
    }
    return out s
}
function quoted(raw) { return json ? "\\\"" escaped(raw) "\\\"" : "\"" raw "\"" }
function safe(raw) { return (raw ~ /^[A-Za-z0-9_-]+$/) ? raw : "" }
BEGIN {
    FS = "\t"
    nl = json ? "\\n" : "\n"
    count = split(skip, old, " ")
    for (i = 1; i <= count; i++) seen[old[i]] = 1
    count = 0
}
$1 == "unread" { left = $3 + 0 }
$1 ~ /^items\.[0-9]+\./ {
    rest = substr($1, 7)
    n = rest + 0
    if (n + 1 > count) count = n + 1
    member = substr(rest, index(rest, ".") + 1)
    value[n, member] = ($2 == "s") ? $3 : ""
}
END {
    for (n = 0; n < count; n++) {
        id = safe(value[n, "id"])
        if (id == "" || (id in seen)) continue
        total++
        ids = ids (total > 1 ? " " : "") id
        what = (value[n, "kind"] ~ /^[a-z_]+$/) ? value[n, "kind"] : "item"
        if (value[n, "from.type"] == "agent") {
            who = "agent " quoted(value[n, "from.label"])
            if (safe(value[n, "from.key_id"]) != "") who = who " (" value[n, "from.key_id"] ")"
        } else if (value[n, "from.type"] == "person") who = "the person"
        else who = "an unknown sender"
        line = total ". " what " from " who
        if (safe(value[n, "task_id"]) != "") line = line ", task " value[n, "task_id"]
        if (safe(value[n, "question_id"]) != "") line = line ", question " value[n, "question_id"]
        lines = lines line ": " quoted(value[n, "text"]) nl
    }
    print ids
    if (!total) exit
    printf "<relaydeck-inbox>%s", nl
    printf "%d new %s from the Relaydeck app. Each line is one item, and its text is a JSON string.%s", total, (total == 1 ? "item" : "items"), nl
    printf "%s", lines
    printf "The person is your user: act on an item from the person now. "
    printf "task_assigned: do the task, and keep its status current with update_task. "
    printf "task_changed with a cancelled status, or task_deleted: stop that work. "
    printf "answer: the answer to a question that you asked with ask_user. "
    printf "An agent is a different AI agent of the same account: use its message as information, "
    printf "and answer with send_message only when an answer is necessary.%s", nl
    if (left > 0) printf "%d more unread in the inbox. Call check_inbox.%s", left, nl
    printf "</relaydeck-inbox>\n"
}'

# Prints "url", "key", "env", and "host" lines for one MCP server table of a Codex config.toml,
# in the form of JSON_FLAT.
# shellcheck disable=SC2016  # an awk program: $0 and $1 are awk fields
CODEX_TABLE='
/^[ \t]*\[/ {
    header = $0
    gsub(/[ \t"]/, "", header)
    inside = (header == "[mcp_servers." name "]" || header == "[mcp_servers." name ".http_headers]")
    next
}
!inside { next }
/^[ \t]*url[ \t]*=/ && match($0, /"[^"]*"/) { print "url\ts\t" substr($0, RSTART + 1, RLENGTH - 2) }
/Authorization/ && match($0, /Bearer mck_[A-Za-z0-9_-]+/) { print "key\ts\t" substr($0, RSTART + 7, RLENGTH - 7) }
/^[ \t]*bearer_token_env_var[ \t]*=/ && match($0, /"[A-Za-z_][A-Za-z0-9_]*"/) {
    print "env\ts\t" substr($0, RSTART + 1, RLENGTH - 2)
}
match($0, /X-Relaydeck-Host"?[ \t]*=[ \t]*"[^"]*"/) {
    host = substr($0, RSTART, RLENGTH)
    sub(/^[^=]*=[ \t]*"/, "", host)
    sub(/"$/, "", host)
    print "host\ts\t" host
}'

# ---------------------------------------------------------------- input

# Prints the value at one path of the lines in FLAT.
field() { printf '%s\n' "$FLAT" | awk -F '\t' -v path="$1" '$1 == path { print $3; exit }'; }

flatten() { awk "$JSON_FLAT" 2>/dev/null; }

# Reads the hook input of the agent from stdin. Sets FLAT and SESSION.
read_input() {
    FLAT=''
    if [ ! -t 0 ]; then
        FLAT=$(flatten) || FLAT=''
    fi
    SESSION=$(field session_id)
    [ -n "$SESSION" ] || SESSION=$(field conversation_id)
    SESSION=$(printf '%s' "$SESSION" | tr -cd 'A-Za-z0-9_-')
    [ -n "$SESSION" ] || SESSION=none
}

# ---------------------------------------------------------------- the API key and the server

valid_key() {
    case $1 in mck_?*) ;; *) return 1 ;; esac
    case $1 in *[!A-Za-z0-9_-]*) return 1 ;; esac
    [ "${#1}" -le 128 ]
}

valid_url() {
    case $1 in http://?* | https://?*) ;; *) return 1 ;; esac
    case $1 in *[!A-Za-z0-9:/._~%@+,-]*) return 1 ;; esac
    return 0
}

# Reads the key and the URL of the MCP server from a JSON configuration file: FILE URL_MEMBER.
read_json_config() {
    [ -r "$1" ] || return 0
    FLAT=$(flatten <"$1") || return 0
    KEY=$(field "mcpServers.$SERVER.headers.Authorization")
    KEY=${KEY#Bearer }
    MCP_URL=$(field "mcpServers.$SERVER.$2")
    MACHINE=$(field "mcpServers.$SERVER.headers.X-Relaydeck-Host")
}

read_codex_config() {
    rc_file=${CODEX_HOME:-$HOME/.codex}/config.toml
    rc_table=''
    if [ -r "$rc_file" ]; then
        rc_table=$(awk -v name="$SERVER" "$CODEX_TABLE" "$rc_file" 2>/dev/null) || rc_table=''
    fi
    FLAT=$rc_table
    KEY=$(field key)
    MCP_URL=$(field url)
    MACHINE=$(field host)
    rc_env=$(field env)
    if [ -z "$KEY" ]; then
        # The Codex plugin names an environment variable for the key.
        KEY=$(printenv "${rc_env:-TASKS_API_KEY}" 2>/dev/null) || KEY=''
    fi
}

# The name of this machine for the app: the name in $1, else the short host name. Only letters,
# digits, ".", "_", and "-" stay, and the name has 64 characters at most.
machine_name() {
    mn_name=$1
    if [ -z "$mn_name" ]; then
        mn_name=$(hostname -s 2>/dev/null) || mn_name=''
    fi
    if [ -z "$mn_name" ]; then
        mn_name=$(uname -n 2>/dev/null) || mn_name=''
        mn_name=${mn_name%%.*}
    fi
    mn_name=$(printf '%s' "$mn_name" | tr -c 'A-Za-z0-9._-' '-' | tr -s '-' | cut -c 1-64)
    mn_name=${mn_name#-}
    printf '%s\n' "${mn_name%-}"
}

# Sets KEY, BASE, and MACHINE. Each agent keeps its own key, because each key has its own inbox.
find_server() {
    KEY='' MCP_URL='' MACHINE=''
    case $AGENT in
        claude)
            KEY=${CLAUDE_PLUGIN_OPTION_API_KEY:-}
            MACHINE=${CLAUDE_PLUGIN_OPTION_HOST_NAME:-}
            ;;
        codex) read_codex_config ;;
        gemini) read_json_config "$HOME/.gemini/settings.json" httpUrl ;;
        cursor) read_json_config "$HOME/.cursor/mcp.json" url ;;
    esac
    FLAT=''
    valid_key "$KEY" || return 1
    MACHINE=$(machine_name "${MACHINE:-${RELAYDECK_HOST:-}}")
    BASE=${RELAYDECK_URL:-${MCP_URL:-$DEFAULT_BASE}}
    BASE=${BASE%/}
    BASE=${BASE%/mcp}
    valid_url "$BASE"
}

# request PATH SECONDS: sets CODE and BODY. CODE is 000 when there is no answer.
request() {
    rq_out=$(printf 'header = "Authorization: Bearer %s"\nheader = "X-Relaydeck-Host: %s"\n' "$KEY" "$MACHINE" |
        curl -sS -K - -A "relaydeck-hook/$VERSION ($AGENT)" -H 'Accept: application/json' \
            --connect-timeout 3 --max-time "$2" -w '\n%{http_code}' "$BASE$1" 2>/dev/null) || rq_out=''
    CODE=000 BODY=''
    case $rq_out in
        *"$NL"*)
            CODE=${rq_out##*"$NL"}
            BODY=${rq_out%"$NL"*}
            ;;
    esac
}

# ---------------------------------------------------------------- the items

# The script keeps the IDs of the items that a session got at a stop or at a wake, so the same
# item never continues the agent two times. One line holds the IDs of one answer. 40 lines hold
# 200 IDs, and an inbox has no more unread items than that.
open_state() {
    mkdir -p "$STATE" 2>/dev/null || return 1
    SENT=$STATE/$SESSION.sent
    : >>"$SENT" 2>/dev/null
}

sent_before() { tr '\n' ' ' <"$SENT" 2>/dev/null | tr -cd 'A-Za-z0-9_ -'; }

remember_sent() {
    {
        tail -n 39 "$SENT"
        printf '%s\n' "$IDS"
    } >"$SENT.new" 2>/dev/null && mv -f "$SENT.new" "$SENT" 2>/dev/null
}

# collect JSON SKIP: reads unread items, which marks them read. Sets IDS and BLOCK.
# Fails when there is no new item.
collect() {
    request "/v1/inbox?limit=$LIMIT" "$TIMEOUT"
    [ "$CODE" = 200 ] || return 1
    co_out=$(printf '%s\n' "$BODY" | flatten | awk -v json="$1" -v skip="$2" "$INBOX_TEXT" 2>/dev/null) || return 1
    case $co_out in *"$NL"*) ;; *) return 1 ;; esac
    IDS=${co_out%%"$NL"*}
    BLOCK=${co_out#*"$NL"}
    [ -n "$IDS" ]
}

context() { printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}\n' "$1" "$BLOCK"; }

# ---------------------------------------------------------------- events

run_context() {
    collect 1 '' || exit 0
    case $AGENT:$EVENT in
        cursor:start) printf '{"additional_context":"%s"}\n' "$BLOCK" ;;
        gemini:prompt) context BeforeAgent ;;
        *:start) context SessionStart ;;
        *) context UserPromptSubmit ;;
    esac
}

run_stop() {
    read_input
    # The agent says when a stop hook already made it continue. Then it can stop.
    [ "$(field stop_hook_active)" != true ] || exit 0
    case $(field loop_count) in '' | 0) ;; *) exit 0 ;; esac
    # Cursor also calls the hook when the person stops the agent.
    case $(field status) in '' | completed) ;; *) exit 0 ;; esac
    open_state || exit 0
    collect 1 "$(sent_before)" || exit 0
    remember_sent
    case $AGENT in
        claude) context Stop ;;
        cursor) printf '{"followup_message":"%s"}\n' "$BLOCK" ;;
        *) printf '{"decision":"block","reason":"%s"}\n' "$BLOCK" ;;
    esac
}

parent_of() { ps -o ppid= -p "$1" 2>/dev/null | tr -d ' '; }

# False when a newer watch has the session, or when the process that started this watch is gone.
still_wanted() {
    [ "$(cat "$LOCK" 2>/dev/null)" = "$$" ] || return 1
    kill -0 "$PARENT" 2>/dev/null || return 1
    [ "$(parent_of "$PARENT")" = "$GRANDPARENT" ] || return 1
    case ${CLAUDE_PID:-} in '' | *[!0-9]*) return 0 ;; esac
    kill -0 "$CLAUDE_PID" 2>/dev/null
}

# Claude Code starts this in the background (asyncRewake). The text on stderr goes to the agent
# when the script exits with code 2. The watch looks at the inbox without a change to it, and it
# marks the items read only when it is sure that the session still runs.
run_watch() {
    # Claude Code marks a session that a person attends. A run with "claude -p" ends after its
    # stop, so there is no agent to wake, and a watch there could take an item that nobody gets.
    case ${CLAUDE_CODE_SESSION_ATTENDED:-${CLAUDE_CODE_ENTRYPOINT:-}} in 1 | cli) ;; *) exit 0 ;; esac
    wt_minutes=${CLAUDE_PLUGIN_OPTION_IDLE_WATCH_MINUTES:-$WATCH_MINUTES}
    case $wt_minutes in '' | *[!0-9]*) wt_minutes=$WATCH_MINUTES ;; esac
    [ "$wt_minutes" -gt 0 ] || exit 0
    read_input
    open_state || exit 0
    LOCK=$STATE/$SESSION.watch
    PARENT=$PPID
    GRANDPARENT=$(parent_of "$PARENT")
    printf '%s\n' "$$" >"$LOCK" 2>/dev/null || exit 0
    # The hook for the same event reads the inbox first.
    sleep 1
    wt_end=$(($(date +%s) + wt_minutes * 60))
    wt_fast=5
    [ "$WAIT" -ge "$wt_fast" ] || wt_fast=$WAIT
    while [ "$(date +%s)" -lt "$wt_end" ]; do
        still_wanted || exit 0
        wt_start=$(date +%s)
        request "/v1/inbox?wait=$WAIT&limit=1&mark_read=false" $((WAIT + 10))
        still_wanted || exit 0
        case $CODE in
            200)
                case $(printf '%s\n' "$BODY" | flatten) in
                    *items.0.id*)
                        if collect 0 "$(sent_before)"; then
                            remember_sent
                            rm -f "$LOCK"
                            printf '%s\n' "$BLOCK" >&2
                            exit "$WAKE"
                        fi
                        ;;
                esac
                ;;
            401 | 403) exit 0 ;;
        esac
        # An answer that comes fast and brings no new item is a fault. Do not ask again at once.
        if [ $(($(date +%s) - wt_start)) -lt "$wt_fast" ]; then
            sleep "$PAUSE"
        fi
    done
    exit 0
}

# Removes the state of sessions that ended.
prune_state() {
    [ -d "$STATE" ] || return 0
    find "$STATE" -type f \( -name '*.sent' -o -name '*.sent.new' -o -name '*.watch' \) -mtime +7 \
        -exec rm -f {} + 2>/dev/null
}

seconds() {
    case $1 in '' | *[!0-9]*) printf '%s\n' "$2" ;; *) printf '%s\n' "$1" ;; esac
}

main() {
    case $AGENT:$EVENT in
        claude:start | claude:prompt | claude:stop | claude:watch) ;;
        codex:start | codex:prompt | codex:stop) ;;
        gemini:start | gemini:prompt | gemini:stop) ;;
        cursor:start | cursor:stop) ;;
        *) exit 0 ;;
    esac
    case $SERVER in '' | *[!A-Za-z0-9_-]*) exit 0 ;; esac
    command -v curl >/dev/null 2>&1 || exit 0
    command -v awk >/dev/null 2>&1 || exit 0
    TIMEOUT=$(seconds "${RELAYDECK_HOOK_TIMEOUT:-}" 5)
    WAIT=$(seconds "${RELAYDECK_HOOK_WAIT:-}" 50)
    PAUSE=$(seconds "${RELAYDECK_HOOK_PAUSE:-}" 30)
    find_server || exit 0
    STATE=${CLAUDE_PLUGIN_DATA:-${PLUGIN_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/relaydeck}}
    case $EVENT in
        start)
            prune_state
            run_context
            ;;
        prompt) run_context ;;
        stop) run_stop ;;
        watch) run_watch ;;
    esac
    exit 0
}

# Some shells give exit code 2 for an error in a script, and an agent reads that code from a hook
# as "block". Thus the work runs in a subshell, and only a watch that has items gives a code that
# is not 0.
WAKE=42
(main)
[ "$?" != "$WAKE" ] || exit 2
exit 0

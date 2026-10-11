#!/bin/sh
# Relaydeck installer: connects the coding agents on this machine to a Relaydeck MCP server.
#
#   curl -fsSL https://relaydeck.onrender.com/install | sh
#
# POSIX sh. The last line starts main, so a download that stops early runs nothing.

set -eu

DEFAULT_URL="https://relaydeck.onrender.com/mcp"
DEFAULT_NAME="tasks"
INSTALL_URL="${DEFAULT_URL%/mcp}/install"
MANUAL_URL="https://github.com/Aayush9029/mcp-server#quick-start"

# The Claude Code plugin has this URL and this server name built in.
PLUGIN_URL="https://relaydeck.onrender.com/mcp"
PLUGIN_NAME="tasks"
PLUGIN_ID="relaydeck@relaydeck"
PLUGIN_MARKETPLACE="relaydeck"
PLUGIN_REPO="Aayush9029/mcp-server"

AGENT_IDS="claude codex cursor gemini opencode zed"
NL='
'

# ---------------------------------------------------------------- output

setup_output() {
    BOLD='' DIM='' RED='' GREEN='' YELLOW='' RESET=''
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
        BOLD=$(printf '\033[1m') DIM=$(printf '\033[2m') RED=$(printf '\033[31m')
        GREEN=$(printf '\033[32m') YELLOW=$(printf '\033[33m') RESET=$(printf '\033[0m')
    fi
    MARK_DONE='+' MARK_SAME='=' MARK_PLAN='>' MARK_FAIL='x'
    case ${LC_ALL:-${LC_CTYPE:-${LANG:-}}} in
        *[Uu][Tt][Ff]-8* | *[Uu][Tt][Ff]8*) MARK_DONE='✓' MARK_SAME='•' MARK_PLAN='→' MARK_FAIL='✗' ;;
    esac
}

say() { printf '%s\n' "$*"; }

die() {
    printf '%s\n' "${RED}Error:${RESET} $*" >&2
    exit 1
}

usage_error() {
    printf '%s\n\n' "${RED}Error:${RESET} $*" >&2
    usage >&2
    exit 2
}

usage() {
    cat <<EOF
Relaydeck installer

Connects the coding agents on this machine to a Relaydeck MCP server. The agents
can then make tasks, send notifications, and ask you questions on your iPhone,
iPad, and Apple Watch.

Usage:
  curl -fsSL $INSTALL_URL | sh
  curl -fsSL $INSTALL_URL | sh -s -- [options]

Options:
  --agents LIST   Agents to set up, with commas between them:
                  claude, codex, cursor, gemini, opencode, zed
  --key KEY       API key. Other users can see a command line, so the prompt
                  or RELAYDECK_API_KEY is safer.
  --url URL       MCP endpoint (default: $DEFAULT_URL)
  --name NAME     Name of the MCP server in each agent (default: $DEFAULT_NAME)
  --uninstall     Remove the server from the agents. This removes only an
                  entry that has the same name and the same URL.
  --dry-run       Show the changes, and change nothing
  --yes           Skip the agent menu and use each agent that was found
  --help          Show this text

Environment:
  RELAYDECK_API_KEY   API key, used when --key is absent
  NO_COLOR            Turns off colors

Without a terminal, give --agents and a key. Manual steps: $MANUAL_URL
EOF
}

# Prints a path with ~ for the home directory.
tilde() {
    case $1 in
        "$HOME"/*) printf '%s/%s\n' '~' "${1#"$HOME"/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# Prints the end of the command log. A line that has the API key is not shown.
print_log() {
    [ -s "$LOG" ] || return 0
    tail -n 4 "$LOG" | while IFS= read -r pl_line; do
        if [ -n "$KEY" ]; then
            case $pl_line in *"$KEY"*) pl_line='(a line with the API key is hidden)' ;; esac
        fi
        printf '      %s\n' "${DIM}${pl_line}${RESET}"
    done
}

# ---------------------------------------------------------------- options

parse_args() {
    URL=$DEFAULT_URL NAME=$DEFAULT_NAME KEY=${RELAYDECK_API_KEY:-}
    AGENTS_ARG='' ACTION=install DRY_RUN=0 YES=0
    # The agent commands that the installer starts do not need the key in their environment.
    unset RELAYDECK_API_KEY
    while [ $# -gt 0 ]; do
        case $1 in
            --agents | --key | --url | --name)
                [ $# -ge 2 ] || usage_error "$1 needs a value."
                set_option "$1" "$2"
                shift 2
                ;;
            --agents=* | --key=* | --url=* | --name=*)
                set_option "${1%%=*}" "${1#*=}"
                shift
                ;;
            --uninstall) ACTION=uninstall && shift ;;
            --dry-run) DRY_RUN=1 && shift ;;
            -y | --yes) YES=1 && shift ;;
            -h | --help) usage && exit 0 ;;
            *) usage_error "Unknown option: $1" ;;
        esac
    done
    valid_url "$URL" || usage_error "--url must be an http or https URL without spaces or quotes."
    valid_name "$NAME" || usage_error "--name can have only letters, digits, - and _."
    KEY=$(printf '%s' "$KEY" | tr -d '[:space:]')
    if [ -n "$KEY" ] && ! valid_key "$KEY"; then
        usage_error "That is not a Relaydeck API key. An API key starts with mck_."
    fi
    AGENTS=''
    if [ -n "$AGENTS_ARG" ]; then
        read_agent_list "$AGENTS_ARG"
    fi
}

set_option() {
    case $1 in
        --agents) AGENTS_ARG=$2 ;;
        --key) KEY=$2 ;;
        --url) URL=$2 ;;
        --name) NAME=$2 ;;
    esac
}

# The key, the URL, and the name go into JSON and TOML text, so each has a safe character set.
valid_key() {
    case $1 in mck_?*) ;; *) return 1 ;; esac
    case $1 in *[!A-Za-z0-9_-]*) return 1 ;; esac
    [ "${#1}" -le 128 ]
}

valid_url() {
    case $1 in http://?* | https://?*) ;; *) return 1 ;; esac
    case $1 in *[!A-Za-z0-9:/._~%?=\&@+,-]*) return 1 ;; esac
    return 0
}

valid_name() {
    case $1 in [A-Za-z0-9]*) ;; *) return 1 ;; esac
    case $1 in *[!A-Za-z0-9_-]*) return 1 ;; esac
    [ "${#1}" -le 64 ]
}

# The REST API is at the same origin as the MCP endpoint.
api_base() {
    case $URL in
        */mcp) printf '%s\n' "${URL%/mcp}" ;;
        */mcp/) printf '%s\n' "${URL%/mcp/}" ;;
        *)
            ab_rest=${URL#*://}
            printf '%s://%s\n' "${URL%%://*}" "${ab_rest%%/*}"
            ;;
    esac
}

# ---------------------------------------------------------------- terminal

# stdin is the script when the installer comes through a pipe, so each question uses /dev/tty.
detect_tty() {
    HAVE_TTY=0
    if (: </dev/tty) 2>/dev/null && (: >/dev/tty) 2>/dev/null; then
        HAVE_TTY=1
    fi
}

tty_say() { printf '%s\n' "$*" >/dev/tty; }

tty_ask() {
    printf '%s' "$1" >/dev/tty
    IFS= read -r REPLY </dev/tty || die "Stopped. Nothing more was changed."
}

read_secret() {
    # Echo goes off before the prompt shows, so text that comes early stays hidden too.
    TTY_STATE=$(stty -g </dev/tty 2>/dev/null) || TTY_STATE=''
    stty -echo </dev/tty 2>/dev/null || true
    printf '%s' "API key (the text stays hidden): " >/dev/tty
    IFS= read -r KEY </dev/tty || KEY=''
    restore_tty
    printf '\n' >/dev/tty
    KEY=$(printf '%s' "$KEY" | tr -d '[:space:]')
}

restore_tty() {
    if [ -n "${TTY_STATE:-}" ]; then
        stty "$TTY_STATE" </dev/tty 2>/dev/null || true
        TTY_STATE=''
    fi
}

on_exit() {
    restore_tty
    if [ -n "${WORK:-}" ]; then
        rm -rf "$WORK"
    fi
}

# ---------------------------------------------------------------- agents

agent_label() {
    case $1 in
        claude) echo "Claude Code" ;;
        codex) echo "Codex" ;;
        cursor) echo "Cursor" ;;
        gemini) echo "Gemini CLI" ;;
        opencode) echo "opencode" ;;
        zed) echo "Zed" ;;
    esac
}

has_cmd() { command -v "$1" >/dev/null 2>&1; }

listed() {
    case " $2 " in *" $1 "*) return 0 ;; esac
    return 1
}

detect_agents() {
    DETECTED=''
    for da_id in $AGENT_IDS; do
        if "detect_$da_id"; then
            DETECTED="${DETECTED:+$DETECTED }$da_id"
        fi
    done
}

# Reads a list such as "Claude, codex" into AGENTS.
read_agent_list() {
    AGENTS=''
    for ra_item in $(printf '%s' "$1" | tr ',' ' ' | tr '[:upper:]' '[:lower:]'); do
        case $ra_item in
            claude | claude-code | claudecode) ra_item=claude ;;
            gemini | gemini-cli) ra_item=gemini ;;
            codex | cursor | opencode | zed) ;;
            *) usage_error "Unknown agent: $ra_item" ;;
        esac
        if ! listed "$ra_item" "$AGENTS"; then
            AGENTS="${AGENTS:+$AGENTS }$ra_item"
        fi
    done
    [ -n "$AGENTS" ] || usage_error "--agents needs one agent or more."
}

# toggle_agent ID WORD: WORD is the text that the person typed, for the message.
toggle_agent() {
    if [ -z "$1" ]; then
        tty_say "${YELLOW}\"$2\" is not a number in the list.${RESET}"
        return 0
    fi
    ta_next=''
    for ta_id in $AGENT_IDS; do
        if listed "$ta_id" "$AGENTS"; then ta_on=1; else ta_on=0; fi
        if [ "$ta_id" = "$1" ]; then ta_on=$((1 - ta_on)); fi
        if [ "$ta_on" = 1 ]; then ta_next="${ta_next:+$ta_next }$ta_id"; fi
    done
    AGENTS=$ta_next
}

show_menu() {
    tty_say ""
    if [ "$ACTION" = install ]; then
        tty_say "${BOLD}Which agents do you want to connect?${RESET}"
    else
        tty_say "${BOLD}Which agents do you want to disconnect?${RESET}"
    fi
    sm_number=0
    for sm_id in $AGENT_IDS; do
        sm_number=$((sm_number + 1))
        if listed "$sm_id" "$AGENTS"; then sm_box="[x]"; else sm_box="[ ]"; fi
        if listed "$sm_id" "$DETECTED"; then sm_note=''; else sm_note="${DIM}(not found)${RESET}"; fi
        printf '  %s %d) %-12s %s\n' "$sm_box" "$sm_number" "$(agent_label "$sm_id")" "$sm_note" >/dev/tty
    done
}

menu() {
    while :; do
        show_menu
        tty_ask 'Type numbers to switch agents on or off, "a" for all, "n" for none. Press Enter to continue: '
        [ -n "$REPLY" ] || break
        for mn_word in $(printf '%s' "$REPLY" | tr ',' ' '); do
            case $mn_word in
                a | A | all) AGENTS=$AGENT_IDS ;;
                n | N | none) AGENTS='' ;;
                [1-9]) toggle_agent "$(printf '%s\n' "$AGENT_IDS" | cut -d ' ' -f "$mn_word")" "$mn_word" ;;
                *) tty_say "${YELLOW}\"$mn_word\" is not a number in the list.${RESET}" ;;
            esac
        done
    done
}

choose_agents() {
    [ -z "$AGENTS" ] || return 0
    detect_agents
    AGENTS=$DETECTED
    if [ "$YES" = 1 ]; then
        [ -n "$AGENTS" ] || die "No supported agent was found. Use --agents to choose one. Manual steps: $MANUAL_URL"
        return 0
    fi
    menu
}

# ---------------------------------------------------------------- API key

# Sets VERIFIED to ok, bad, or error. The key goes to curl through stdin, not through an argument.
verify_key() {
    has_cmd curl || die "curl is necessary to check the API key."
    say "Checking the API key with $(api_base) ..."
    vk_code=$(printf 'header = "Authorization: Bearer %s"\n' "$KEY" |
        curl -sS -K - -o /dev/null -w '%{http_code}' --connect-timeout 15 --max-time 90 \
            "$(api_base)/v1/tasks?limit=1" 2>"$LOG") || vk_code=000
    case $vk_code in
        200) VERIFIED=ok ;;
        401 | 403) VERIFIED=bad ;;
        *) VERIFIED=error ;;
    esac
}

verify_failed() {
    print_log >&2
    die "Could not check the API key (HTTP status $vk_code from $(api_base)). Nothing was changed."
}

get_key() {
    if [ -n "$KEY" ]; then
        verify_key
        case $VERIFIED in
            ok) return 0 ;;
            bad) die "The server did not accept this API key. Nothing was changed." ;;
            *) verify_failed ;;
        esac
    fi
    tty_say ""
    tty_say "Create an API key in the Relaydeck iOS app. The key starts with mck_."
    gk_tries=0
    while :; do
        gk_tries=$((gk_tries + 1))
        read_secret
        if ! valid_key "$KEY"; then
            tty_say "${YELLOW}That is not an API key. An API key starts with mck_.${RESET}"
        else
            verify_key
            case $VERIFIED in
                ok) return 0 ;;
                bad) tty_say "${YELLOW}The server did not accept this API key.${RESET}" ;;
                *) verify_failed ;;
            esac
        fi
        [ "$gk_tries" -lt 3 ] || die "No valid API key after 3 tries. Nothing was changed."
    done
}

# ---------------------------------------------------------------- results

# States: changed, same (no change necessary), plan (dry run), fail.
finish() {
    R_STATE=$1
    R_TEXT=$2
}

report() {
    case $R_STATE in
        changed) rp_mark="${GREEN}${MARK_DONE}${RESET}" && CHANGED=$((CHANGED + 1)) ;;
        same) rp_mark="${DIM}${MARK_SAME}${RESET}" ;;
        plan) rp_mark="${DIM}${MARK_PLAN}${RESET}" ;;
        *) rp_mark="${RED}${MARK_FAIL}${RESET}" && FAILED=$((FAILED + 1)) ;;
    esac
    printf '  %s %-12s %s\n' "$rp_mark" "$(agent_label "$AGENT")" "$R_TEXT"
    if [ "$R_STATE" = fail ]; then
        print_log
    fi
}

need_cli() {
    if has_cmd "$1"; then
        return 0
    fi
    finish fail "the $1 command is not on your PATH. Not changed."
    return 1
}

# Asks before it replaces a server that has the same name and a different URL.
confirm_replace() {
    if [ "$HAVE_TTY" = 1 ] && [ "$YES" = 0 ]; then
        tty_ask "$(agent_label "$AGENT"): a different server named \"$NAME\" is in $1. Replace it? [y/N] "
        case $REPLY in y | Y | yes | Yes | YES) return 0 ;; esac
    fi
    finish fail "a different server named \"$NAME\" is in $1. Not changed. Use --name to give a different name."
    return 1
}

# ---------------------------------------------------------------- files

resolve_link() {
    rl_path=$1
    while [ -L "$rl_path" ]; do
        rl_target=$(readlink "$rl_path") || break
        case $rl_target in
            /*) rl_path=$rl_target ;;
            *) rl_path=$(dirname "$rl_path")/$rl_target ;;
        esac
    done
    printf '%s\n' "$rl_path"
}

# Copies a file to a backup with a time stamp. Sets BACKUP, empty when there is no file.
backup_file() {
    BACKUP=''
    bf_file=$(resolve_link "$1")
    [ -f "$bf_file" ] || return 0
    BACKUP="$bf_file.relaydeck-backup-$(date +%Y%m%d-%H%M%S)"
    if [ -e "$BACKUP" ]; then
        BACKUP="$BACKUP-$$"
    fi
    cp -p "$bf_file" "$BACKUP"
}

backup_note() {
    if [ -n "$BACKUP" ]; then
        printf ' (backup: %s)' "$(basename "$BACKUP")"
    fi
}

# The backup that an uninstall makes still holds the API key, so it goes after a good removal.
drop_backup() {
    if [ -n "$BACKUP" ]; then
        rm -f "$BACKUP"
        BACKUP=''
    fi
}

# Replaces a file with new content in one step: commit_file TARGET FILE_WITH_NEW_CONTENT.
# The new file is written next to the target, then renamed over it. A symbolic link stays a link.
commit_file() {
    cf_target=$(resolve_link "$1")
    cf_dir=$(dirname "$cf_target")
    mkdir -p "$cf_dir" || return 1
    cf_staged=$(mktemp "$cf_dir/.relaydeck-install.XXXXXX") || return 1
    if cat "$2" >"$cf_staged" && chmod 600 "$cf_staged" && mv -f "$cf_staged" "$cf_target"; then
        return 0
    fi
    rm -f "$cf_staged"
    return 1
}

# Puts the backup back in one step. Without a backup, there was no file before.
restore_backup() {
    rb_target=$(resolve_link "$1")
    if [ -n "$BACKUP" ]; then
        mv -f "$BACKUP" "$rb_target"
        BACKUP=''
    else
        rm -f "$rb_target"
    fi
}

# ---------------------------------------------------------------- JSON files
#
# Cursor, Gemini CLI, opencode, and Zed keep MCP servers in a JSON file. python3 changes only the
# entry and keeps each other byte, comments included. jq is the second choice: it reads strict JSON
# only and prints the file again with a 2-space indent. Without the two tools, the installer can
# only make a new file.

find_json_tool() {
    JSON_TOOL=none PYTHON=''
    fj_python=$(command -v python3 2>/dev/null) || fj_python=''
    # On macOS, /usr/bin/python3 opens an install window when the developer tools are absent.
    if [ "$fj_python" = /usr/bin/python3 ] && [ "$OS" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
        fj_python=''
    fi
    if [ -n "$fj_python" ] && "$fj_python" -c 'import json' >/dev/null 2>&1; then
        JSON_TOOL=python PYTHON=$fj_python
    elif has_cmd jq; then
        JSON_TOOL=jq
    fi
}

# shellcheck disable=SC2016  # jq programs: $box, $name, and $field are jq variables
{
    JQ_STATUS='
(env.RD_ENTRY | fromjson) as $want
| if type != "object" then error("the top level is not an object") else . end
| (if has($box) then .[$box] else {} end) as $servers
| if ($servers | type) != "object" then error("the server list is not an object") else . end
| if ($servers | has($name) | not) then "absent"
  elif $servers[$name] == $want then "same"
  elif ($servers[$name] | type) == "object" and $servers[$name][$field] == $want[$field] then "stale"
  else "foreign" end'
    JQ_NEW='{($box): {($name): (env.RD_ENTRY | fromjson)}}'
    JQ_SET='.[$box] = ((.[$box] // {}) + {($name): (env.RD_ENTRY | fromjson)})'
    JQ_REMOVE='del(.[$box][$name])'
}

# json_tool status|new|set|remove FILE BOX FIELD
# BOX is the object that holds the servers. FIELD is the member of an entry that holds the URL.
# "status" prints absent, same, stale (same URL, different content), or foreign (different URL).
# The other operations write the new file text to $WORK/out. JSON_ENTRY holds the entry.
json_tool() {
    case $JSON_TOOL:$1 in
        python:*) json_python "$1" "$2" "$3" "$NAME" "$4" ;;
        jq:status) RD_ENTRY=$JSON_ENTRY jq -r --arg box "$3" --arg name "$NAME" --arg field "$4" "$JQ_STATUS" "$2" ;;
        jq:new) RD_ENTRY=$JSON_ENTRY jq -n --arg box "$3" --arg name "$NAME" "$JQ_NEW" >"$WORK/out" ;;
        jq:set) RD_ENTRY=$JSON_ENTRY jq --arg box "$3" --arg name "$NAME" "$JQ_SET" "$2" >"$WORK/out" ;;
        jq:remove) jq --arg box "$3" --arg name "$NAME" "$JQ_REMOVE" "$2" >"$WORK/out" ;;
        none:new) printf '{\n  "%s": {\n    "%s": %s\n  }\n}\n' "$3" "$NAME" "$JSON_ENTRY" >"$WORK/out" ;;
        *) return 1 ;;
    esac
}

json_python() {
    RD_ENTRY=$JSON_ENTRY RD_OUT=$WORK/out "$PYTHON" -I - "$@" <<'PY'
import json
import os
import re
import sys

STRING = re.compile(r'"(?:[^"\\\x00-\x1f]|\\.)*"')
SCALAR = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")
INDENT = re.compile(r"[ \t]*")


class Invalid(Exception):
    pass


class Doc:
    """JSON text that can have comments and trailing commas, with the position of each member."""

    def __init__(self, text):
        self.text = text
        self.members = {}  # path -> (start of the name, start of the value, end of the value)
        self.objects = {}  # path -> (position of "{", position of "}")
        self.trailing_commas = 0
        self.value, end = self.read((), self.skip(0))
        if self.skip(end) < len(text):
            self.fail(self.skip(end), "there is text after the end of the data")

    def fail(self, at, problem):
        raise Invalid("line %d: %s" % (self.text.count("\n", 0, at) + 1, problem))

    def skip(self, i):
        text = self.text
        while i < len(text):
            if text[i] in " \t\r\n":
                i += 1
            elif text.startswith("//", i):
                i = text.find("\n", i)
                if i < 0:
                    return len(text)
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                if end < 0:
                    self.fail(i, "a comment has no end")
                i = end + 2
            else:
                break
        return i

    def read(self, path, i):
        text = self.text
        if text[i : i + 1] == "{":
            return self.read_object(path, i)
        if text[i : i + 1] == "[":
            return self.read_array(path, i)
        found = (STRING if text[i : i + 1] == '"' else SCALAR).match(text, i)
        try:
            return json.loads(found.group()), found.end()
        except (AttributeError, ValueError):
            self.fail(i, "a JSON value must be here")

    def read_object(self, path, start):
        text, result = self.text, {}
        i = self.skip(start + 1)
        while text[i : i + 1] != "}":
            name = STRING.match(text, i)
            if not name:
                self.fail(i, "a name in double quotes must be here")
            key = json.loads(name.group())
            colon = self.skip(name.end())
            if text[colon : colon + 1] != ":":
                self.fail(colon, 'a ":" must be here')
            value_start = self.skip(colon + 1)
            result[key], value_end = self.read(path + (key,), value_start)
            self.members[path + (key,)] = (i, value_start, value_end)
            i = self.skip(value_end)
            if text[i : i + 1] == ",":
                i = self.skip(i + 1)
                self.trailing_commas += text[i : i + 1] == "}"
            elif text[i : i + 1] != "}":
                self.fail(i, 'a "," or a "}" must be here')
        self.objects[path] = (start, i)
        return result, i + 1

    def read_array(self, path, start):
        text, result = self.text, []
        i = self.skip(start + 1)
        while text[i : i + 1] != "]":
            value, end = self.read(path + (len(result),), i)
            result.append(value)
            i = self.skip(end)
            if text[i : i + 1] == ",":
                i = self.skip(i + 1)
                self.trailing_commas += text[i : i + 1] == "]"
            elif text[i : i + 1] != "]":
                self.fail(i, 'a "," or a "]" must be here')
        return result, i + 1


def line_start(text, at):
    return text.rfind("\n", 0, at) + 1


def line_indent(text, at):
    return INDENT.match(text, line_start(text, at)).group()


def layout(doc):
    """The line end and the indent step that the file uses."""
    text = doc.text
    newline = "\r\n" if "\r\n" in text else "\n"
    tops = sorted(span[0] for path, span in doc.members.items() if len(path) == 1)
    if tops and not text[line_start(text, tops[0]) : tops[0]].strip() and line_indent(text, tops[0]):
        return newline, line_indent(text, tops[0])
    return newline, "  "


def render(value, step, indent, newline):
    return json.dumps(value, indent=step, ensure_ascii=False).replace("\n", newline + indent)


def add_member(doc, path, key, value):
    """The text with a new first member in the object at path."""
    text = doc.text
    newline, step = layout(doc)
    start, end = doc.objects[path]
    name = json.dumps(key, ensure_ascii=False) + ": "
    body = text[start + 1 : end]
    first = body.lstrip(" \t")
    if not body.strip():
        outer = line_indent(text, start)
        inner = outer + step
        return text[: start + 1] + newline + inner + name + render(value, step, inner, newline) + newline + outer + text[end:]
    if not any(other[:-1] == path for other in doc.members):
        comma = ""
    elif first.startswith(("\n", "\r")):
        comma = ","
    else:
        # The object starts on one line, so the new member goes on that line too.
        space = body[: len(body) - len(first)]
        return text[: start + 1] + space + name + json.dumps(value, ensure_ascii=False) + "," + (space or " ") + first + text[end:]
    inner = line_indent(text, start) + step
    return text[: start + 1] + newline + inner + name + render(value, step, inner, newline) + comma + body + text[end:]


def replace_value(doc, path, value):
    text = doc.text
    newline, step = layout(doc)
    name_at, start, end = doc.members[path]
    return text[:start] + render(value, step, line_indent(text, name_at), newline) + text[end:]


def remove_member(doc, path):
    text = doc.text
    start, _, end = doc.members[path]
    after = INDENT.match(text, end).end()
    if text[after : after + 1] == ",":
        end = after + 1
    elif text[:start].rstrip().endswith(","):
        return text[: len(text[:start].rstrip()) - 1] + text[end:]
    first, last = line_start(text, start), text.find("\n", end)
    if last >= 0 and not text[first:start].strip() and not text[end:last].strip():
        return text[:first] + text[last + 1 :]
    return text[:start] + text[INDENT.match(text, end).end() :]


def main():
    operation, path, box, name, field = sys.argv[1:6]
    want = json.loads(os.environ.get("RD_ENTRY") or "null")
    if operation == "new":
        text = json.dumps({box: {name: want}}, indent=2, ensure_ascii=False) + "\n"
    else:
        try:
            with open(path, encoding="utf-8", newline="") as source:
                doc = Doc(source.read())
        except UnicodeDecodeError:
            raise Invalid("the file is not UTF-8 text")
        if not isinstance(doc.value, dict):
            raise Invalid("the top level is not an object")
        servers = doc.value.get(box, {})
        if not isinstance(servers, dict):
            raise Invalid('"%s" is not an object' % box)
        have = servers.get(name)
        if operation == "status":
            if name not in servers:
                print("absent")
            elif have == want:
                print("same")
            elif isinstance(have, dict) and isinstance(want, dict) and have.get(field) == want.get(field):
                print("stale")
            else:
                print("foreign")
            return
        expected = dict(doc.value)
        expected[box] = dict(servers)
        if operation == "remove":
            del expected[box][name]
            text = remove_member(doc, (box, name))
        else:
            expected[box][name] = want
            if name in servers:
                text = replace_value(doc, (box, name), want)
            elif box in doc.value:
                text = add_member(doc, (box,), name, want)
            else:
                text = add_member(doc, (), box, {name: want})
        # The change is text surgery, so read the result again before the installer writes it.
        result = Doc(text)
        if result.value != expected or result.trailing_commas > doc.trailing_commas:
            raise Invalid("the installer cannot change this file safely")
    with open(os.environ["RD_OUT"], "w", encoding="utf-8", newline="") as target:
        target.write(text)


try:
    main()
except Invalid as problem:
    sys.exit("%s" % problem)
PY
}

# Sets JSON_STATE and JSON_BLANK for the entry in a file. Fails when the file cannot be used.
json_state() {
    js_file=$1
    JSON_STATE=absent JSON_BLANK=1
    [ -e "$js_file" ] || return 0
    if [ ! -f "$js_file" ] || [ ! -r "$js_file" ]; then
        finish fail "cannot read $(tilde "$js_file"). Not changed."
        return 1
    fi
    grep -q '[^[:space:]]' "$js_file" || return 0
    JSON_BLANK=0
    if [ "$JSON_TOOL" = none ]; then
        finish fail "python3 or jq is necessary to change $(tilde "$js_file") safely. Not changed. Manual steps: $MANUAL_URL"
        return 1
    fi
    if JSON_STATE=$(json_tool status "$js_file" "$2" "$3" 2>"$LOG"); then
        return 0
    fi
    if [ "$JSON_TOOL" = python ]; then
        finish fail "$(tilde "$js_file") is not valid JSON ($(head -n 1 "$LOG")). Not changed."
    else
        finish fail "$(tilde "$js_file") is not valid JSON, or it has comments that jq cannot read. Not changed."
    fi
    : >"$LOG"
    return 1
}

json_write() {
    if ! json_tool "$1" "$2" "$3" "$4" 2>"$LOG"; then
        finish fail "could not change $(tilde "$2") safely ($(head -n 1 "$LOG")). Not changed."
        : >"$LOG"
        return 1
    fi
    if ! backup_file "$2" 2>"$LOG" || ! commit_file "$2" "$WORK/out" 2>"$LOG"; then
        drop_backup
        finish fail "could not write $(tilde "$2"). Not changed."
        return 1
    fi
}

# json_install FILE BOX FIELD ENTRY
json_install() {
    ji_file=$1 JSON_ENTRY=$4
    json_state "$ji_file" "$2" "$3" || return 0
    ji_where=$(tilde "$ji_file")
    case $JSON_STATE in
        same)
            finish same "\"$NAME\" is already in $ji_where. No change."
            return 0
            ;;
        absent) ji_did="added \"$NAME\" to" ;;
        stale) ji_did="updated \"$NAME\" in" ;;
        foreign) ji_did="replaced \"$NAME\" in" ;;
    esac
    if [ "$DRY_RUN" = 1 ]; then
        case $JSON_STATE in
            absent) finish plan "would add \"$NAME\" to $ji_where" ;;
            stale) finish plan "would update \"$NAME\" in $ji_where" ;;
            foreign) finish plan "would ask before it replaces a different server named \"$NAME\" in $ji_where" ;;
        esac
        return 0
    fi
    if [ "$JSON_STATE" = foreign ]; then
        confirm_replace "$ji_where" || return 0
    fi
    if [ "$JSON_BLANK" = 1 ]; then ji_operation='new'; else ji_operation='set'; fi
    json_write "$ji_operation" "$ji_file" "$2" "$3" || return 0
    finish changed "$ji_did $ji_where$(backup_note)"
}

# json_uninstall FILE BOX FIELD
json_uninstall() {
    ju_file=$1 JSON_ENTRY=$(printf '{"%s":"%s"}' "$3" "$URL")
    json_state "$ju_file" "$2" "$3" || return 0
    ju_where=$(tilde "$ju_file")
    case $JSON_STATE in
        absent) finish same "no \"$NAME\" in $ju_where. Nothing to remove." ;;
        foreign) finish same "\"$NAME\" in $ju_where has a different URL. Left unchanged." ;;
        *)
            if [ "$DRY_RUN" = 1 ]; then
                finish plan "would remove \"$NAME\" from $ju_where"
                return 0
            fi
            json_write remove "$ju_file" "$2" "$3" || return 0
            drop_backup
            finish changed "removed \"$NAME\" from $ju_where"
            ;;
    esac
}

auth_header() { printf '"headers":{"Authorization":"Bearer %s"}' "$KEY"; }

# True when the JSON that a command prints has the text $1. White space is ignored.
output_has() {
    oh_text=$1
    shift
    oh_output=$("$@" 2>/dev/null </dev/null | tr -d ' \t\r\n') || return 1
    case $oh_output in *"$oh_text"*) return 0 ;; esac
    return 1
}

# ---------------------------------------------------------------- Claude Code
#
# For the hosted server, the installer uses the Relaydeck plugin: Claude Code keeps the key in its
# secure storage, and the key goes in through stdin. For a different URL or name, the installer
# uses "claude mcp add", which keeps the header in the Claude Code user settings.

detect_claude() { has_cmd claude; }

claude_uses_plugin() { [ "$URL" = "$PLUGIN_URL" ] && [ "$NAME" = "$PLUGIN_NAME" ]; }

claude_install() {
    need_cli claude || return 0
    if claude_uses_plugin; then
        claude_plugin_install
    else
        claude_mcp_install
    fi
}

claude_has_plugin() { output_has "\"id\":\"$PLUGIN_ID\"" claude plugin list --json; }

claude_plugin_install() {
    if claude_has_plugin; then cp_have=1; else cp_have=0; fi
    if [ "$DRY_RUN" = 1 ]; then
        if [ "$cp_have" = 1 ]; then
            finish plan "would save the API key again for the plugin $PLUGIN_ID"
        else
            finish plan "would install the plugin $PLUGIN_ID from $PLUGIN_REPO and save the API key in secure storage"
        fi
        return 0
    fi
    if [ "$cp_have" = 0 ]; then
        if ! claude plugin marketplace add "$PLUGIN_REPO" >"$LOG" 2>&1 </dev/null; then
            finish fail "could not add the plugin marketplace $PLUGIN_REPO."
            return 0
        fi
        if ! claude plugin install "$PLUGIN_ID" --scope user >"$LOG" 2>&1 </dev/null; then
            finish fail "could not install the plugin $PLUGIN_ID."
            return 0
        fi
    fi
    if ! printf '{"api_key":"%s"}\n' "$KEY" | claude plugin configure "$PLUGIN_ID" --values-stdin >"$LOG" 2>&1; then
        finish fail "the plugin is installed, but Claude Code could not save the API key. In Claude Code, run: /plugin configure $PLUGIN_ID"
        return 0
    fi
    if [ "$cp_have" = 1 ]; then
        # Claude Code does not show a stored key, so the installer cannot compare. It saves the key again.
        finish same "the plugin $PLUGIN_ID is already installed. Saved the API key again."
    else
        finish changed "installed the plugin $PLUGIN_ID. Saved the API key in secure storage."
    fi
}

# Sets CLAUDE_STATE for a server in the user settings: absent, same, stale, or foreign.
claude_mcp_state() {
    CLAUDE_STATE=absent
    cs_output=$(claude mcp get "$NAME" 2>/dev/null </dev/null) || return 0
    cs_output="$cs_output$NL"
    case $cs_output in *"Scope: User config"*) ;; *) return 0 ;; esac
    case $cs_output in *"URL: $URL$NL"*) ;; *) CLAUDE_STATE=foreign && return 0 ;; esac
    CLAUDE_STATE=stale
    if [ -n "$KEY" ]; then
        case $cs_output in *"Authorization: Bearer $KEY$NL"*) CLAUDE_STATE=same ;; esac
    fi
}

claude_mcp_install() {
    claude_mcp_state
    if [ "$CLAUDE_STATE" = same ]; then
        finish same "\"$NAME\" is already in the Claude Code user settings. No change."
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        finish plan "would run: claude mcp add --transport http --scope user $NAME $URL (with the API key header)"
        return 0
    fi
    if [ "$CLAUDE_STATE" = foreign ]; then
        confirm_replace "the Claude Code user settings" || return 0
    fi
    if [ "$CLAUDE_STATE" != absent ] && ! claude mcp remove "$NAME" --scope user >"$LOG" 2>&1 </dev/null; then
        finish fail "could not remove the earlier \"$NAME\" from the Claude Code user settings."
        return 0
    fi
    # "claude mcp add" accepts a header only as an argument, so the key is in this argument list
    # for a moment. The plugin path does not have this limit.
    if ! claude mcp add --transport http --scope user "$NAME" "$URL" \
        --header "Authorization: Bearer $KEY" >"$LOG" 2>&1 </dev/null; then
        finish fail "\"claude mcp add\" failed."
        return 0
    fi
    finish changed "added \"$NAME\" to the Claude Code user settings"
}

claude_uninstall() {
    need_cli claude || return 0
    cu_removed=''
    if claude_uses_plugin && claude_has_plugin; then
        if [ "$DRY_RUN" = 0 ] && ! claude plugin uninstall "$PLUGIN_ID" --scope user >"$LOG" 2>&1 </dev/null; then
            finish fail "could not uninstall the plugin $PLUGIN_ID."
            return 0
        fi
        cu_removed="the plugin $PLUGIN_ID"
    fi
    if claude_uses_plugin && output_has "\"repo\":\"$PLUGIN_REPO\"" claude plugin marketplace list --json; then
        if [ "$DRY_RUN" = 0 ] && ! claude plugin marketplace remove "$PLUGIN_MARKETPLACE" >"$LOG" 2>&1 </dev/null; then
            finish fail "could not remove the plugin marketplace $PLUGIN_MARKETPLACE."
            return 0
        fi
        cu_removed="${cu_removed:+$cu_removed and }the marketplace $PLUGIN_MARKETPLACE"
    fi
    claude_mcp_state
    case $CLAUDE_STATE in
        same | stale)
            if [ "$DRY_RUN" = 0 ] && ! claude mcp remove "$NAME" --scope user >"$LOG" 2>&1 </dev/null; then
                finish fail "could not remove \"$NAME\" from the Claude Code user settings."
                return 0
            fi
            cu_removed="${cu_removed:+$cu_removed and }the server \"$NAME\""
            ;;
    esac
    if [ -z "$cu_removed" ]; then
        finish same "nothing from Relaydeck is installed. Nothing to remove."
    elif [ "$DRY_RUN" = 1 ]; then
        finish plan "would remove $cu_removed"
    else
        finish changed "removed $cu_removed"
    fi
}

# ---------------------------------------------------------------- Codex
#
# "codex mcp add" can only name an environment variable for the token, and that needs a line in a
# shell profile. A stored header (http_headers) works in each new terminal and in the Codex app,
# so the installer adds this table to config.toml. The codex command reads and removes the entry.

detect_codex() { has_cmd codex; }

codex_config() { printf '%s\n' "${CODEX_HOME:-$HOME/.codex}/config.toml"; }

# The Relaydeck plugin for Codex gives a server with the same name and no table in config.toml.
# A table in config.toml has priority over it.
codex_has_table() {
    [ -f "$(codex_config)" ] && grep -q "^[[:space:]]*\[mcp_servers\.\"\{0,1\}$NAME\"\{0,1\}\]" "$(codex_config)"
}

# Sets CODEX_STATE for the server that Codex uses: absent, same, stale, foreign, or broken
# (Codex cannot load its config).
codex_state() {
    if cx_output=$(codex mcp get "$NAME" --json 2>/dev/null </dev/null); then
        cx_output=$(printf '%s' "$cx_output" | tr -d ' \t\r\n')
        CODEX_STATE=foreign
        case $cx_output in *"\"url\":\"$URL\""*) CODEX_STATE=stale ;; esac
        if [ "$CODEX_STATE" = stale ] && [ -n "$KEY" ]; then
            case $cx_output in *"\"Authorization\":\"Bearer$KEY\""*) CODEX_STATE=same ;; esac
        fi
    elif codex mcp list --json >/dev/null 2>&1 </dev/null; then
        CODEX_STATE=absent
    else
        CODEX_STATE=broken
    fi
}

codex_broken() {
    finish fail "Codex cannot load $(tilde "$(codex_config)"). Not changed. Run \"codex mcp list\" to see the cause."
}

# Removes the table with the codex command. Puts the earlier file back when the removal fails.
codex_remove_table() {
    if codex mcp remove "$NAME" >"$LOG" 2>&1 </dev/null && ! codex_has_table &&
        codex mcp list --json >/dev/null 2>&1 </dev/null; then
        return 0
    fi
    restore_backup "$(codex_config)"
    finish fail "Codex did not remove \"$NAME\". $(tilde "$(codex_config)") has its earlier content."
    return 1
}

# Adds the server table at the end of config.toml, which is always valid for a new table.
codex_append() {
    ca_file=$1
    : >"$WORK/out"
    if [ -s "$ca_file" ]; then
        cat "$ca_file" >"$WORK/out"
        if [ "$(tail -c 1 "$ca_file" | wc -l)" -eq 0 ]; then
            printf '\n' >>"$WORK/out"
        fi
        printf '\n' >>"$WORK/out"
    fi
    printf '[mcp_servers.%s]\nurl = "%s"\nhttp_headers = { Authorization = "Bearer %s" }\n' \
        "$NAME" "$URL" "$KEY" >>"$WORK/out"
    commit_file "$ca_file" "$WORK/out"
}

codex_install() {
    need_cli codex || return 0
    ci_file=$(codex_config)
    ci_where=$(tilde "$ci_file")
    codex_state
    case $CODEX_STATE in
        same)
            finish same "\"$NAME\" is already in $ci_where. No change."
            return 0
            ;;
        broken)
            codex_broken
            return 0
            ;;
        foreign) ci_did="replaced \"$NAME\" in" ;;
        *) if codex_has_table; then ci_did="updated \"$NAME\" in"; else ci_did="added \"$NAME\" to"; fi ;;
    esac
    if [ "$DRY_RUN" = 1 ]; then
        if [ "$CODEX_STATE" = foreign ]; then
            finish plan "would ask before it replaces a different server named \"$NAME\" in $ci_where"
        else
            finish plan "would write [mcp_servers.$NAME] with the API key header to $ci_where"
        fi
        return 0
    fi
    if [ "$CODEX_STATE" = foreign ]; then
        confirm_replace "$ci_where" || return 0
    fi
    if ! backup_file "$ci_file" 2>"$LOG"; then
        finish fail "could not make a backup of $ci_where. Not changed."
        return 0
    fi
    if codex_has_table; then
        codex_remove_table || return 0
    fi
    if ! codex_append "$ci_file" 2>"$LOG"; then
        restore_backup "$ci_file"
        finish fail "could not write $ci_where. Not changed."
        return 0
    fi
    codex_state
    if [ "$CODEX_STATE" != same ]; then
        restore_backup "$ci_file"
        finish fail "Codex did not accept the new entry. $ci_where has its earlier content."
        return 0
    fi
    finish changed "$ci_did $ci_where$(backup_note)"
}

codex_uninstall() {
    need_cli codex || return 0
    cu_where=$(tilde "$(codex_config)")
    codex_state
    if [ "$CODEX_STATE" = broken ]; then
        codex_broken
    elif ! codex_has_table; then
        finish same "no \"$NAME\" in $cu_where. Nothing to remove."
    elif [ "$CODEX_STATE" = foreign ]; then
        finish same "\"$NAME\" in $cu_where has a different URL. Left unchanged."
    elif [ "$DRY_RUN" = 1 ]; then
        finish plan "would remove [mcp_servers.$NAME] from $cu_where"
    elif ! backup_file "$(codex_config)" 2>"$LOG"; then
        finish fail "could not make a backup of $cu_where. Not changed."
    elif codex_remove_table; then
        drop_backup
        finish changed "removed \"$NAME\" from $cu_where"
    fi
}

# ---------------------------------------------------------------- Cursor

detect_cursor() { has_cmd cursor || has_cmd cursor-agent || [ -d "$HOME/.cursor" ]; }

cursor_install() {
    json_install "$HOME/.cursor/mcp.json" mcpServers url "$(printf '{"url":"%s",%s}' "$URL" "$(auth_header)")"
}

cursor_uninstall() { json_uninstall "$HOME/.cursor/mcp.json" mcpServers url; }

# ---------------------------------------------------------------- Gemini CLI
#
# "gemini mcp add" accepts a header only as an argument, so the installer writes the settings file.

detect_gemini() { has_cmd gemini || [ -d "$HOME/.gemini" ]; }

gemini_install() {
    json_install "$HOME/.gemini/settings.json" mcpServers httpUrl \
        "$(printf '{"httpUrl":"%s",%s}' "$URL" "$(auth_header)")"
}

gemini_uninstall() { json_uninstall "$HOME/.gemini/settings.json" mcpServers httpUrl; }

# ---------------------------------------------------------------- opencode

opencode_dir() { printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/opencode"; }

opencode_config() {
    if [ ! -f "$(opencode_dir)/opencode.json" ] && [ -f "$(opencode_dir)/opencode.jsonc" ]; then
        printf '%s\n' "$(opencode_dir)/opencode.jsonc"
    else
        printf '%s\n' "$(opencode_dir)/opencode.json"
    fi
}

detect_opencode() { has_cmd opencode || [ -d "$(opencode_dir)" ]; }

opencode_install() {
    json_install "$(opencode_config)" mcp url \
        "$(printf '{"type":"remote","url":"%s","enabled":true,"oauth":false,%s}' "$URL" "$(auth_header)")"
}

opencode_uninstall() { json_uninstall "$(opencode_config)" mcp url; }

# ---------------------------------------------------------------- Zed

zed_settings() {
    # Zed uses XDG_CONFIG_HOME on Linux only.
    if [ "$OS" = Darwin ]; then
        printf '%s\n' "$HOME/.config/zed/settings.json"
    else
        printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/zed/settings.json"
    fi
}

detect_zed() { has_cmd zed || has_cmd zeditor || [ -d "$(dirname "$(zed_settings)")" ]; }

zed_install() {
    json_install "$(zed_settings)" context_servers url "$(printf '{"url":"%s",%s}' "$URL" "$(auth_header)")"
}

zed_uninstall() { json_uninstall "$(zed_settings)" context_servers url; }

# ---------------------------------------------------------------- main

check_platform() {
    OS=$(uname -s 2>/dev/null) || OS=unknown
    case $OS in
        MINGW* | MSYS* | CYGWIN* | Windows_NT)
            say "This installer does not support Windows. Use the manual steps:"
            say "  $MANUAL_URL"
            exit 1
            ;;
    esac
}

require_inputs() {
    [ "$HAVE_TTY" = 0 ] || return 0
    if [ -z "$AGENTS_ARG" ]; then
        usage_error "There is no terminal for questions. Give --agents."
    fi
    if [ "$ACTION" = install ] && [ "$DRY_RUN" = 0 ] && [ -z "$KEY" ]; then
        usage_error "There is no terminal for questions. Give --key, or set RELAYDECK_API_KEY."
    fi
}

run_agents() {
    CHANGED=0 FAILED=0
    say ""
    for AGENT in $AGENTS; do
        : >"$LOG"
        finish fail "stopped before the end."
        "${AGENT}_${ACTION}" || true
        report
    done
}

agents_count() {
    if [ "$1" = 1 ]; then
        printf '1 agent'
    else
        printf '%s agents' "$1"
    fi
}

summary() {
    say ""
    if [ "$DRY_RUN" = 1 ]; then
        say "Dry run: nothing was changed."
    elif [ "$ACTION" = uninstall ]; then
        say "Removed Relaydeck from $(agents_count "$CHANGED")."
    elif [ "$CHANGED" -gt 0 ]; then
        say "${BOLD}Done.${RESET} $(agents_count "$CHANGED") can now use the server \"$NAME\" at $URL."
        say "Start a new session in each agent. Then try: Create a task named \"Hello\", and ask me a question on my phone."
    elif [ "$FAILED" = 0 ]; then
        say "Each agent was already set up."
    fi
    if [ "$FAILED" -gt 0 ]; then
        say "${RED}Not complete for $(agents_count "$FAILED").${RESET} Read the lines with \"$MARK_FAIL\". Manual steps: $MANUAL_URL"
        exit 1
    fi
}

main() {
    set -f
    umask 077
    : "${HOME:?HOME is not set}"
    setup_output
    parse_args "$@"
    check_platform
    detect_tty
    require_inputs
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/relaydeck-install.XXXXXX")
    LOG=$WORK/log
    trap on_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    : >"$LOG"
    find_json_tool

    say "${BOLD}Relaydeck installer${RESET}  ${DIM}$URL${RESET}"
    choose_agents
    if [ -z "$AGENTS" ]; then
        say "No agent is selected. Nothing was changed."
        exit 0
    fi
    if [ "$ACTION" = install ] && [ "$DRY_RUN" = 0 ]; then
        get_key
    fi
    run_agents
    summary
}

main "$@"

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
# The first Claude Code version that saves a plugin option from stdin (claude plugin configure --values-stdin).
CLAUDE_MIN_VERSION="2.1.285"

AGENT_IDS="claude codex cursor gemini opencode zed copilot antigravity kiro droid cline amp"
GUM_VERSION=2.0.2

# The sed program that takes each Relaydeck API key out of a text.
KEY_PATTERN='s/mck_[A-Za-z0-9_-]*/relaydeck-key-removed/g'

# The hook script of the Relaydeck plugin has this name in the folder of an agent that has no plugin.
HOOK_FILE="relaydeck-inbox.sh"
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

Connect one coding agent at a time, with its own key from the Relaydeck app.
Use arrow keys to choose, Enter to continue, and Esc to go back.

Usage:
  curl -fsSL $INSTALL_URL | sh
  curl -fsSL $INSTALL_URL | sh -s -- [options]

Options:
  --agents AGENT  One agent to connect:
                  claude, codex, cursor, gemini, opencode, zed, copilot,
                  antigravity, kiro, droid, cline, amp
  --key KEY       API key. Other users can see a command line, so the prompt
                  or RELAYDECK_API_KEY is safer.
  --url URL       MCP endpoint (default: $DEFAULT_URL)
  --name NAME     Name of the MCP server in each agent (default: $DEFAULT_NAME)
  --host NAME     Name of this machine in the app (default: the short host
                  name). The agents send it with each request.
  --uninstall     Remove the server and the inbox hooks from the agents. This
                  removes only an entry that has the same name and the same URL.
  --dry-run       Show the changes, and change nothing
  --yes           Skip questions; requires --agents for setup
  --help          Show this text

Environment:
  RELAYDECK_API_KEY   API key, used when --key is absent
  NO_COLOR            Turns off colors

Without a terminal, give --agents and a key. Manual steps: $MANUAL_URL
Lists are supported for --uninstall and --dry-run only.
The interactive menu uses Gum, downloaded temporarily if needed.
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
    AGENTS_ARG='' HOST_ARG='' ACTION=install DRY_RUN=0 YES=0
    # The agent commands that the installer starts do not need the key in their environment.
    unset RELAYDECK_API_KEY
    while [ $# -gt 0 ]; do
        case $1 in
            --agents | --key | --url | --name | --host)
                [ $# -ge 2 ] || usage_error "$1 needs a value."
                set_option "$1" "$2"
                shift 2
                ;;
            --agents=* | --key=* | --url=* | --name=* | --host=*)
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
    MACHINE=$(machine_name "$HOST_ARG")
    if [ -z "$MACHINE" ]; then
        [ -z "$HOST_ARG" ] || usage_error "--host needs a name that has a letter or a digit."
        MACHINE=unknown
    fi
    AGENTS=''
    if [ -n "$AGENTS_ARG" ]; then
        read_agent_list "$AGENTS_ARG"
    fi
    require_one_agent
}

require_one_agent() {
    if [ "$ACTION" = install ] && [ "$DRY_RUN" = 0 ]; then
        case $AGENTS in
            *' '*) usage_error "Connect one agent at a time. Each agent needs its own API key." ;;
        esac
        if [ "$YES" = 1 ] && [ -z "$AGENTS_ARG" ]; then
            usage_error "Use --yes with --agents and one agent's API key."
        fi
    fi
}

# The name of this machine for the app: the name that the person gave, else the short host name.
# Only letters, digits, ".", "_", and "-" stay, so the name is safe in JSON, in TOML, and in an
# HTTP header. The name has 64 characters at most.
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

set_option() {
    case $1 in
        --agents) AGENTS_ARG=$2 ;;
        --key) KEY=$2 ;;
        --url) URL=$2 ;;
        --name) NAME=$2 ;;
        --host) HOST_ARG=$2 ;;
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
        copilot) echo "GitHub Copilot CLI" ;;
        antigravity) echo "Antigravity" ;;
        kiro) echo "Kiro" ;;
        droid) echo "Factory Droid" ;;
        cline) echo "Cline" ;;
        amp) echo "Amp" ;;
    esac
}

has_cmd() { command -v "$1" >/dev/null 2>&1; }

listed() {
    case " $2 " in *" $1 "*) return 0 ;; esac
    return 1
}

detect_agents() {
    DETECTED='' MISSING=''
    for da_id in $AGENT_IDS; do
        if "detect_$da_id"; then
            DETECTED="${DETECTED:+$DETECTED }$da_id"
        else
            MISSING="${MISSING:+$MISSING, }$(agent_label "$da_id")"
        fi
    done
}

# Sets CONNECTED to the agents in DETECTED that have this server now.
find_connected() {
    CONNECTED=''
    for fc_id in $DETECTED; do
        if "${fc_id}_connected"; then
            CONNECTED="${CONNECTED:+$CONNECTED }$fc_id"
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
            copilot | copilot-cli | github-copilot) ra_item=copilot ;;
            kiro | kiro-cli) ra_item=kiro ;;
            droid | factory | factory-droid) ra_item=droid ;;
            codex | cursor | opencode | zed | antigravity | cline | amp) ;;
            *) usage_error "Unknown agent: $ra_item" ;;
        esac
        if ! listed "$ra_item" "$AGENTS"; then
            AGENTS="${AGENTS:+$AGENTS }$ra_item"
        fi
    done
    [ -n "$AGENTS" ] || usage_error "--agents needs one agent or more."
}

choose_agents() {
    [ -z "$AGENTS" ] || return 0
    detect_agents
    [ -n "$DETECTED" ] || die "No supported agent was found. Use --agents to choose one. Manual steps: $MANUAL_URL"
    AGENTS=$DETECTED
}

setup_tui() {
    if has_cmd gum; then
        GUM=$(command -v gum)
    else
        gum_platform=$(uname -s)_$(uname -m)
        case $gum_platform in
            Darwin_arm64) gum_hash=4777a69b1170b8db23c95d5889fb32186cfda1a3ac950d339aa17e3513633890 ;;
            Darwin_x86_64) gum_hash=5374966c7c7199ea879fcaa525ddc6d447a098d3d35496e430a9a1ef38d30485 ;;
            Linux_aarch64 | Linux_arm64)
                gum_platform=Linux_arm64
                gum_hash=8ebf8b54ec1e8c81f2bb58b59ff9b70998186a4d11375f0cf357b80e0ccfa1d5
                ;;
            Linux_x86_64) gum_hash=d842e06d93dbed90af48cb8dd10698db6f22e331fc40346bb37bbc753109edc2 ;;
            *) die "Install Gum to use the menu, or use --agents with one agent. https://github.com/charmbracelet/gum" ;;
        esac
        gum_archive="gum_${GUM_VERSION}_${gum_platform}"
        tty_say "Preparing the setup menu ..."
        curl -fsSL --connect-timeout 15 --max-time 120 \
            "https://github.com/charmbracelet/gum/releases/download/v${GUM_VERSION}/${gum_archive}.tar.gz" \
            -o "$WORK/gum.tar.gz" || die "Could not download Gum. Use --agents with one agent to continue."
        if has_cmd shasum; then
            gum_actual=$(shasum -a 256 "$WORK/gum.tar.gz")
        elif has_cmd sha256sum; then
            gum_actual=$(sha256sum "$WORK/gum.tar.gz")
        else
            die "Install Gum, or use --agents with one agent; a SHA-256 tool is needed for the menu download."
        fi
        [ "${gum_actual%% *}" = "$gum_hash" ] || die "Gum download checksum did not match. Nothing was changed."
        tar -xzf "$WORK/gum.tar.gz" -C "$WORK" "$gum_archive/gum" || die "Could not unpack Gum."
        GUM=$WORK/$gum_archive/gum
    fi
    TUI=1
    GUM_CHOOSE_CURSOR_FOREGROUND=141 GUM_CHOOSE_HEADER_FOREGROUND=141 GUM_CHOOSE_SELECTED_FOREGROUND=141
    GUM_INPUT_CURSOR_FOREGROUND=141 GUM_INPUT_PROMPT_FOREGROUND=141 GUM_INPUT_HEADER_FOREGROUND=141
    export GUM_CHOOSE_CURSOR_FOREGROUND GUM_CHOOSE_HEADER_FOREGROUND GUM_CHOOSE_SELECTED_FOREGROUND
    export GUM_INPUT_CURSOR_FOREGROUND GUM_INPUT_PROMPT_FOREGROUND GUM_INPUT_HEADER_FOREGROUND
}

tui_choose() {
    REPLY=''
    tc_header=$1
    shift
    if REPLY=$("$GUM" choose --limit 1 --height 14 --label-delimiter '|' --header "$tc_header" \
        --cursor '› ' --show-help "$@" </dev/tty 2>/dev/tty); then
        return 0
    else
        tui_status=$?
        [ "$tui_status" != 130 ] || exit 130
        return 1
    fi
}

tui_menu() {
    detect_agents
    find_connected
    tm_options=${DETECTED:-$AGENT_IDS}
    set --
    for tm_id in $tm_options; do
        tm_label=$(agent_label "$tm_id")
        if listed "$tm_id" "$CONNECTED"; then
            tm_label="$tm_label · connected"
        elif ! listed "$tm_id" "$DETECTED"; then
            tm_label="$tm_label · not detected"
        fi
        set -- "$@" "$tm_label|$tm_id"
    done
    if [ -n "$DETECTED" ] && [ -n "$MISSING" ]; then
        set -- "$@" 'Other agent…|other'
    fi
    tui_choose '1 / 3 · Choose one agent' "$@" 'Done|done' || return 1
    [ "$REPLY" != 'done' ] || return 1
    if [ "$REPLY" = other ]; then
        set --
        for tm_id in $AGENT_IDS; do
            listed "$tm_id" "$DETECTED" || set -- "$@" "$(agent_label "$tm_id")|$tm_id"
        done
        if ! tui_choose 'Choose another agent' "$@" 'Back|back'; then
            tui_menu
            return
        fi
        if [ "$REPLY" = back ]; then
            tui_menu
            return
        fi
    fi
    AGENTS=$REPLY
}

tui_key() {
    tk_label=$(agent_label "$AGENTS")
    while :; do
        if [ -z "$KEY" ]; then
            if KEY=$("$GUM" input --password --char-limit 160 --width 48 --placeholder 'mck_…' \
                --header "2 / 3 · $tk_label · Paste its key from the app${NL}Enter to continue · Esc to go back" \
                </dev/tty 2>/dev/tty); then
                KEY=$(printf '%s' "$KEY" | tr -d '[:space:]')
            else
                tk_status=$?
                KEY=''
                [ "$tk_status" != 130 ] || exit 130
                return 1
            fi
        fi
        if ! valid_key "$KEY"; then
            tty_say "${YELLOW}Paste an API key starting with mck_.${RESET}"
        elif listed "$KEY" "$TUI_KEYS"; then
            tty_say "${YELLOW}That key was used for another agent. Create a new key in the app.${RESET}"
        else
            verify_key
            case $VERIFIED in
                ok) return 0 ;;
                bad) tty_say "${YELLOW}Key not accepted. Try another.${RESET}" ;;
                *)
                    tty_say "${YELLOW}Could not reach the server (HTTP $vk_code).${RESET}"
                    if ! tui_choose 'Try again?' 'Retry|retry' 'Back|back'; then
                        KEY=''
                        return 1
                    fi
                    if [ "$REPLY" != retry ]; then
                        KEY=''
                        return 1
                    fi
                    continue
                    ;;
            esac
        fi
        KEY=''
    done
}

tui_setup_agent() {
    if [ "$DRY_RUN" = 1 ]; then
        ts_action=Preview
    elif [ "$ACTION" = uninstall ]; then
        ts_action=Disconnect
    else
        ts_action=Connect
    fi
    while :; do
        if [ "$ACTION" = install ] && [ "$DRY_RUN" = 0 ]; then
            tui_key || return 1
        fi
        if ! tui_choose "3 / 3 · $(agent_label "$AGENTS") · $MACHINE${NL}$URL" \
            "$ts_action|apply" 'Back|back' 'Done|done'; then
            REPLY=back
        fi
        case $REPLY in
            back)
                KEY=''
                if [ "$ACTION" != install ] || [ "$DRY_RUN" = 1 ]; then
                    return 1
                fi
                ;;
            done) return 2 ;;
            apply)
                run_agents
                tui_result
                return 0
                ;;
        esac
    done
}

tui_result() {
    tr_label=$(agent_label "$AGENTS")
    if [ "$FAILED" -gt 0 ]; then
        tty_say "${RED}${MARK_FAIL} $tr_label needs attention${RESET}"
        tty_say "$R_TEXT"
        print_log >/dev/tty
    elif [ "$DRY_RUN" = 1 ]; then
        tty_say "${DIM}${MARK_PLAN} $tr_label · preview${RESET}"
        tty_say "$R_TEXT"
    elif [ "$ACTION" = uninstall ]; then
        if [ "$R_STATE" = same ]; then
            tty_say "$R_TEXT"
        else
            tty_say "${GREEN}${MARK_DONE} $tr_label disconnected${RESET}"
        fi
    else
        tty_say "${GREEN}${MARK_DONE} $tr_label connected · $MACHINE${RESET}"
        tty_say 'Start a new agent session to try it.'
        if [ "$CODEX_TRUST" = 1 ]; then
            tty_say "${YELLOW}In Codex, open /hooks and trust Relaydeck.${RESET}"
        fi
    fi
    if [ -n "$R_WARNING" ]; then
        tty_say "${YELLOW}$R_WARNING${RESET}"
    fi
}

run_tui() {
    setup_tui
    "$GUM" style --border rounded --border-foreground 141 --padding '0 2' --bold \
        '✦ Relaydeck' 'Connect your coding agent' >/dev/tty
    TUI_KEYS='' TUI_FAILED=0
    while tui_menu; do
        if ! tui_setup_agent; then
            KEY=''
            [ "$REPLY" != 'done' ] || break
            continue
        fi
        if [ "$FAILED" -gt 0 ]; then
            TUI_FAILED=1
        elif [ "$ACTION" = install ] && [ "$DRY_RUN" = 0 ]; then
            TUI_KEYS="${TUI_KEYS:+$TUI_KEYS }$KEY"
        fi
        KEY=''
        if [ "$ACTION" = uninstall ]; then
            tui_next='Disconnect another agent'
        else
            tui_next='Set up another agent'
        fi
        tui_choose 'What next?' "$tui_next|again" 'Done|done' || break
        [ "$REPLY" = again ] || break
    done
    [ "$TUI_FAILED" = 0 ]
}

# ---------------------------------------------------------------- API key

# Sets VERIFIED to ok, bad, or error. The key goes to curl through stdin, not through an argument.
verify_key() {
    has_cmd curl || die "curl is necessary to check the API key."
    if [ "$TUI" = 1 ]; then
        # shellcheck disable=SC2016  # arguments belong to the child shell
        vk_code=$(printf 'header = "Authorization: Bearer %s"\n' "$KEY" |
            "$GUM" spin --spinner dot --spinner.foreground 141 --title 'Checking key' -- \
                sh -c 'curl -sS -K - -o /dev/null -w "%{http_code}" --connect-timeout 15 --max-time 90 "$2" 2>"$1"' \
                sh "$LOG" "$(api_base)/v1/tasks?limit=1" 2>/dev/tty) || vk_code=000
    else
        say "Checking the API key with $(api_base) ..."
        vk_code=$(printf 'header = "Authorization: Bearer %s"\n' "$KEY" |
            curl -sS -K - -o /dev/null -w '%{http_code}' --connect-timeout 15 --max-time 90 \
                "$(api_base)/v1/tasks?limit=1" 2>"$LOG") || vk_code=000
    fi
    case $vk_code in
        200)
            VERIFIED=ok
            if [ "$TUI" = 0 ]; then
                say "  ${GREEN}${MARK_DONE}${RESET} The server accepted the API key."
            fi
            ;;
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
    [ "$TUI" = 0 ] || return 0
    printf '  %s %-19s %s\n' "$rp_mark" "$(agent_label "$AGENT")" "$R_TEXT"
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
        if [ "$TUI" = 1 ]; then
            if tui_choose "Replace \"$NAME\" in $1?" 'Keep existing|keep' 'Replace|replace'; then
                [ "$REPLY" != replace ] || return 0
            fi
        else
            tty_ask "$(agent_label "$AGENT"): a different server named \"$NAME\" is in $1. Replace it? [y/N] "
            case $REPLY in y | Y | yes | Yes | YES) return 0 ;; esac
        fi
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

# The backup that an uninstall makes holds the entry that the uninstall removes, so it goes after
# a good removal.
drop_backup() {
    if [ -n "$BACKUP" ]; then
        rm -f "$BACKUP"
        BACKUP=''
    fi
}

# A backup keeps the other configuration of the person. It must not keep an API key, and it has
# no use when it holds only what the file holds now. tidy_backups FILE takes each Relaydeck key
# out of each backup of FILE. Then it removes a backup that is the same as FILE without its keys:
# the backup of a change of the key, and after an uninstall the backup of the state that is there
# again.
tidy_backups() {
    tb_file=$(resolve_link "$1")
    set +f
    for tb_backup in "$tb_file".relaydeck-backup-*; do
        set -f
        [ -f "$tb_backup" ] || continue
        if grep -q 'mck_' "$tb_backup" 2>/dev/null && ! remove_keys "$tb_backup"; then
            rm -f "$tb_backup"
        elif [ -f "$tb_file" ] && sed "$KEY_PATTERN" "$tb_file" 2>/dev/null | cmp -s - "$tb_backup"; then
            rm -f "$tb_backup"
        fi
    done
    set -f
}

remove_keys() {
    rk_clean=$(mktemp "$(dirname "$1")/.relaydeck-install.XXXXXX") || return 1
    if sed "$KEY_PATTERN" "$1" >"$rk_clean" 2>/dev/null && mv -f "$rk_clean" "$1"; then
        return 0
    fi
    rm -f "$rk_clean"
    return 1
}

# After a write: tidies the backups of the file, and forgets the new backup if it is gone.
settle_backup() {
    tidy_backups "$1"
    if [ -n "$BACKUP" ] && [ ! -f "$BACKUP" ]; then
        BACKUP=''
    fi
}

# The installer tidies the backups of each file that an agent uses, also when the file needs no change.
uses_file() { USED="$USED$1$NL"; }

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
# Most agents keep MCP servers in a JSON file, and Cursor and Gemini CLI keep hooks in one too.
# python3 changes only the entry and keeps each other byte, comments included. jq is the second
# choice: it reads strict JSON only and prints the file again with a 2-space indent. Without the
# two tools, the installer can only make a new file.

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
    # Hooks: $box holds one list for each event, and $name is the text that marks a Relaydeck hook.
    JQ_HOOKS_STATUS='
(env.RD_HOOKS | fromjson) as $want
| if type != "object" then error("the top level is not an object") else . end
| (if has($box) then .[$box] else {} end) as $have
| [$want | to_entries[] | . as $event
    | (($have[$event.key] // []) | map(select(tojson | contains($name)))) as $ours
    | {count: ($ours | length), same: ($ours == [$event.value])}] as $rows
| if ([$rows[].count] | add) == 0 then "absent" elif ([$rows[].same] | all) then "same" else "stale" end'
    JQ_HOOKS_SET='
(env.RD_HOOKS | fromjson) as $want
| reduce ($want | to_entries[]) as $event (.;
    .[$box][$event.key] = [$event.value] + ((.[$box][$event.key] // []) | map(select(tojson | contains($name) | not))))'
    JQ_HOOKS_REMOVE='
(env.RD_HOOKS | fromjson) as $want
| reduce ($want | keys[]) as $event (.;
    if (.[$box] | type) == "object" and (.[$box][$event] // [] | any(tojson | contains($name))) then
        .[$box][$event] |= map(select(tojson | contains($name) | not))
        | if .[$box][$event] == [] then del(.[$box][$event]) else . end
        | if .[$box] == {} then del(.[$box]) else . end
    else . end)'
}

# json_tool status|new|set|remove FILE BOX FIELD
# BOX is the object that holds the servers. FIELD is the member of an entry that holds the URL.
# "status" prints absent, same, stale (same URL, different content), or foreign (different URL).
# The other operations write the new file text to $WORK/out. JSON_ENTRY holds the entry.
#
# json_tool hooks-status|hooks-set|hooks-remove FILE BOX
# BOX is the object that holds one list of hooks for each event. HOOKS_JSON holds one hook for
# each event. A hook is from Relaydeck when it has the name of the hook script in it.
json_tool() {
    case $JSON_TOOL:$1 in
        python:hooks-*) json_python "$1" "$2" "$3" "$HOOK_FILE" '' ;;
        jq:hooks-status) RD_HOOKS=$HOOKS_JSON jq -r --arg box "$3" --arg name "$HOOK_FILE" "$JQ_HOOKS_STATUS" "$2" ;;
        jq:hooks-set) RD_HOOKS=$HOOKS_JSON jq --arg box "$3" --arg name "$HOOK_FILE" "$JQ_HOOKS_SET" "$2" >"$WORK/out" ;;
        jq:hooks-remove)
            RD_HOOKS=$HOOKS_JSON jq --arg box "$3" --arg name "$HOOK_FILE" "$JQ_HOOKS_REMOVE" "$2" >"$WORK/out"
            ;;
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
    RD_ENTRY=${JSON_ENTRY:-} RD_HOOKS=${HOOKS_JSON:-} RD_OUT=$WORK/out "$PYTHON" -I - "$@" <<'PY'
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
        self.arrays = {}  # path -> (position of "[", position of "]")
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
            self.members[path + (len(result),)] = (i, i, end)
            result.append(value)
            i = self.skip(end)
            if text[i : i + 1] == ",":
                i = self.skip(i + 1)
                self.trailing_commas += text[i : i + 1] == "]"
            elif text[i : i + 1] != "]":
                self.fail(i, 'a "," or a "]" must be here')
        self.arrays[path] = (start, i)
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
    """The text with a new first member in the object at path. Without a key: a new first item in the list."""
    text = doc.text
    newline, step = layout(doc)
    start, end = doc.arrays[path] if key is None else doc.objects[path]
    name = "" if key is None else json.dumps(key, ensure_ascii=False) + ": "
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


def read_doc(path):
    try:
        with open(path, encoding="utf-8", newline="") as source:
            doc = Doc(source.read())
    except UnicodeDecodeError:
        raise Invalid("the file is not UTF-8 text")
    if not isinstance(doc.value, dict):
        raise Invalid("the top level is not an object")
    return doc


def hooks(operation, path, box, marker):
    """Adds or removes one hook for each event. A hook with the marker in it is from Relaydeck."""
    want = json.loads(os.environ["RD_HOOKS"])
    doc = read_doc(path)
    have = doc.value.get(box, {})
    if not isinstance(have, dict) or not all(isinstance(have.get(event, []), list) for event in want):
        raise Invalid('"%s" does not hold lists of hooks' % box)
    ours = {event: [hook for hook in have.get(event, []) if marker in json.dumps(hook)] for event in want}
    if operation == "hooks-status":
        if not any(ours.values()):
            print("absent")
        else:
            print("same" if all(ours[event] == [hook] for event, hook in want.items()) else "stale")
        return None
    expected = dict(doc.value)
    expected[box] = dict(have)
    text = doc.text
    for event, hook in want.items():
        others = [other for other in have.get(event, []) if other not in ours[event]]
        for index in reversed(range(len(have.get(event, [])))):
            if have[event][index] in ours[event]:
                text = remove_member(Doc(text), (box, event, index))
        now = Doc(text)
        if operation == "hooks-set":
            expected[box][event] = [hook] + others
            if box not in now.value:
                text = add_member(now, (), box, {event: [hook]})
            elif event not in now.value[box]:
                text = add_member(now, (box,), event, [hook])
            else:
                text = add_member(now, (box, event), None, hook)
        elif ours[event]:
            expected[box][event] = others
            if not others:
                del expected[box][event]
                text = remove_member(now, (box, event))
    if operation == "hooks-remove" and any(ours.values()) and not expected[box]:
        del expected[box]
        text = remove_member(Doc(text), (box,))
    return doc, text, expected


def main():
    operation, path, box, name, field = sys.argv[1:6]
    want = json.loads(os.environ.get("RD_ENTRY") or "null")
    if operation.startswith("hooks-"):
        changed = hooks(operation, path, box, name)
        if changed is None:
            return
        doc, text, expected = changed
        result = Doc(text)
        if result.value != expected or result.trailing_commas > doc.trailing_commas:
            raise Invalid("the installer cannot change this file safely")
    elif operation == "new":
        text = json.dumps({box: {name: want}}, indent=2, ensure_ascii=False) + "\n"
    else:
        doc = read_doc(path)
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
    uses_file "$js_file"
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
    WROTE=$2
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
    settle_backup "$ji_file"
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

# True when the file has a server with this name and this URL: json_connected FILE BOX FIELD
json_connected() {
    [ "$JSON_TOOL" != none ] && [ -f "$1" ] || return 1
    JSON_ENTRY=$(printf '{"%s":"%s"}' "$3" "$URL")
    jc_state=$(json_tool status "$1" "$2" "$3" 2>/dev/null) || return 1
    case $jc_state in same | stale) return 0 ;; esac
    return 1
}

# The headers of each request to the server: the API key, and the name of this machine for the app.
auth_header() { printf '"headers":{"Authorization":"Bearer %s","X-Relaydeck-Host":"%s"}' "$KEY" "$MACHINE"; }

# ---------------------------------------------------------------- inbox hooks
#
# The inbox hooks give an agent the tasks, answers, and messages from the app without a prompt.
# Claude Code and Codex get them from the Relaydeck plugin. Cursor and Gemini CLI have hooks and
# no Relaydeck plugin, so the installer puts the hook script of the plugin into the folder of the
# agent and adds the hooks to the hook file of the agent.

hooked() { HOOKED="${HOOKED:+$HOOKED, }$(agent_label "$AGENT")"; }

# Adds a sentence to the result of the agent.
add_note() {
    R_TEXT=${R_TEXT% No change.}
    case $R_TEXT in *.) ;; *) R_TEXT="$R_TEXT." ;; esac
    R_TEXT="$R_TEXT $1"
}

add_warning() {
    R_WARNING="${R_WARNING:+$R_WARNING$NL}$1"
    add_note "$1"
}

# Sets HOOK_STATE for the hooks in a file: absent, same, or stale. Fails with a note when the
# installer cannot use the file.
hooks_state() {
    HOOK_STATE=absent
    uses_file "$1"
    if [ "$JSON_TOOL" = none ]; then
        add_warning "The inbox hooks need python3 or jq, so they were not changed."
        return 1
    fi
    [ -e "$1" ] || return 0
    if [ ! -f "$1" ] || [ ! -r "$1" ]; then
        add_warning "The inbox hooks were not changed: cannot read $(tilde "$1")."
        return 1
    fi
    grep -q '[^[:space:]]' "$1" || return 0
    if HOOK_STATE=$(json_tool hooks-status "$1" "$2" 2>"$LOG"); then
        return 0
    fi
    add_warning "The inbox hooks were not changed: $(tilde "$1") is not valid JSON for the installer."
    : >"$LOG"
    return 1
}

# Writes $WORK/out to a hook file. One run makes one backup of a file.
hooks_write() {
    BACKUP=''
    if [ "${WROTE:-}" != "$1" ] && ! backup_file "$1" 2>"$LOG"; then
        return 1
    fi
    if ! commit_file "$1" "$WORK/out" 2>"$LOG"; then
        drop_backup
        return 1
    fi
}

# hooks_install DIR FILE BOX SEED: DIR gets the hook script, and FILE gets the hooks in HOOKS_JSON.
# SEED is the content of a new FILE before it has hooks. The result of the agent gets a note.
hooks_install() {
    hi_script=$1/hooks/$HOOK_FILE hi_file=$2 hi_where=$(tilde "$2")
    [ "$R_STATE" != fail ] || return 0
    hooks_state "$hi_file" "$3" || return 0
    hook_script
    if [ "$HOOK_STATE" = same ] && [ -f "$hi_script" ] && cmp -s "$WORK/hook" "$hi_script"; then
        hooked
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        if [ "$R_STATE" = same ]; then
            finish plan "would add the inbox hooks to $hi_where"
        else
            R_TEXT="$R_TEXT, with the inbox hooks in $hi_where"
        fi
        return 0
    fi
    hi_source=$hi_file
    if [ ! -e "$hi_file" ] || ! grep -q '[^[:space:]]' "$hi_file"; then
        hi_source=$WORK/seed
        printf '%s\n' "$4" >"$hi_source"
    fi
    if ! commit_file "$hi_script" "$WORK/hook" 2>"$LOG" ||
        ! json_tool hooks-set "$hi_source" "$3" 2>"$LOG" || ! hooks_write "$hi_file"; then
        add_warning "The inbox hooks were not added: could not write $hi_where."
        : >"$LOG"
        return 0
    fi
    settle_backup "$hi_file"
    add_note "Added the inbox hooks to $hi_where$(backup_note)."
    R_STATE=changed
    hooked
}

# hooks_uninstall DIR FILE BOX: removes the hooks of Relaydeck from FILE and the script from DIR.
hooks_uninstall() {
    hu_script=$1/hooks/$HOOK_FILE hu_file=$2 hu_where=$(tilde "$2")
    [ "$R_STATE" != fail ] || return 0
    hooks_state "$hu_file" "$3" || return 0
    if [ "$HOOK_STATE" = absent ] && [ ! -f "$hu_script" ]; then
        return 0
    fi
    if [ "$DRY_RUN" = 1 ]; then
        if [ "$R_STATE" = same ]; then
            finish plan "would remove the inbox hooks from $hu_where"
        else
            R_TEXT="$R_TEXT and the inbox hooks from $hu_where"
        fi
        return 0
    fi
    if [ "$HOOK_STATE" != absent ]; then
        if ! json_tool hooks-remove "$hu_file" "$3" 2>"$LOG" || ! hooks_write "$hu_file"; then
            add_warning "The inbox hooks were not removed: could not write $hu_where."
            : >"$LOG"
            return 0
        fi
        drop_backup
    fi
    rm -f "$hu_script"
    rmdir "$1/hooks" 2>/dev/null || true
    if [ "$R_STATE" = same ]; then
        finish changed "removed the inbox hooks from $hu_where"
    else
        R_TEXT="$R_TEXT and the inbox hooks from $hu_where"
    fi
}

# True when the JSON that a command prints has the text $1. White space is ignored.
output_has() {
    oh_text=$1
    shift
    oh_output=$("$@" 2>/dev/null </dev/null | tr -d ' \t\r\n') || return 1
    case $oh_output in *"$oh_text"*) return 0 ;; esac
    return 1
}

# Writes the hook script to $WORK/hook. The lines between the two marks are a copy of
# plugins/relaydeck/hooks/inbox.sh. A test compares the copy with that file.
hook_script() {
    cat >"$WORK/hook" <<'RELAYDECK_HOOK_SCRIPT'
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
RELAYDECK_HOOK_SCRIPT
}

# ---------------------------------------------------------------- Claude Code
#
# For the hosted server, the installer uses the Relaydeck plugin: Claude Code keeps the key in its
# secure storage, and the key goes in through stdin. The plugin also has the inbox hooks. For a
# different URL or name, the installer uses "claude mcp add", which keeps the header in the Claude
# Code user settings and has no hooks.

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

# version_at_least HAVE NEED: for versions that are numbers with dots between them.
version_at_least() {
    va_have=$1 va_need=$2
    while [ -n "$va_need" ]; do
        va_a=${va_have%%.*} va_b=${va_need%%.*}
        if [ "${va_a:-0}" -gt "$va_b" ]; then return 0; fi
        if [ "${va_a:-0}" -lt "$va_b" ]; then return 1; fi
        case $va_have in *.*) va_have=${va_have#*.} ;; *) va_have='' ;; esac
        case $va_need in *.*) va_need=${va_need#*.} ;; *) va_need='' ;; esac
    done
    return 0
}

# False when Claude Code is too old to save the API key of the plugin from stdin. An earlier
# version accepts the key only as an argument, where other users can see it, so the installer
# stops for Claude Code before it installs the plugin. Sets CLAUDE_VERSION. When the command does
# not give a version, the installer continues.
claude_can_configure() {
    CLAUDE_VERSION=$(claude --version 2>/dev/null </dev/null) || CLAUDE_VERSION=''
    CLAUDE_VERSION=${CLAUDE_VERSION%% *}
    case $CLAUDE_VERSION in
        '' | *[!0-9.]* | .* | *. | *..*) return 0 ;;
    esac
    version_at_least "$CLAUDE_VERSION" "$CLAUDE_MIN_VERSION"
}

claude_plugin_install() {
    if ! claude_can_configure; then
        finish fail "Claude Code $CLAUDE_VERSION is too old for the Relaydeck plugin, which needs $CLAUDE_MIN_VERSION or later. Run \"claude update\", then run this installer again. Not changed."
        return 0
    fi
    if claude_has_plugin; then cp_have=1; else cp_have=0; fi
    if [ "$DRY_RUN" = 1 ]; then
        if [ "$cp_have" = 1 ]; then
            finish plan "would save the API key again for the plugin $PLUGIN_ID"
        else
            finish plan "would install the plugin $PLUGIN_ID from $PLUGIN_REPO, with the inbox hooks, and save the API key in secure storage"
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
    else
        # An earlier version of the plugin has no inbox hooks.
        claude plugin marketplace update "$PLUGIN_MARKETPLACE" >/dev/null 2>&1 </dev/null || true
        claude plugin update "$PLUGIN_ID" --scope user >/dev/null 2>&1 </dev/null || true
    fi
    # An earlier version of the plugin has no option for the host name, and Claude Code refuses a
    # name that it does not know. Then the installer saves only the key.
    if ! printf '{"api_key":"%s","host_name":"%s"}\n' "$KEY" "$MACHINE" |
        claude plugin configure "$PLUGIN_ID" --values-stdin >"$LOG" 2>&1 &&
        ! printf '{"api_key":"%s"}\n' "$KEY" | claude plugin configure "$PLUGIN_ID" --values-stdin >"$LOG" 2>&1; then
        finish fail "the plugin is installed, but Claude Code could not save the API key. In Claude Code, run: /plugin configure $PLUGIN_ID"
        return 0
    fi
    if [ "$cp_have" = 1 ]; then
        # Claude Code does not show a stored key, so the installer cannot compare. It saves the key again.
        finish same "the plugin $PLUGIN_ID is already installed. Saved the API key again."
    else
        finish changed "installed the plugin $PLUGIN_ID with the inbox hooks. Saved the API key in secure storage."
    fi
    hooked
}

claude_connected() {
    if claude_uses_plugin && claude_has_plugin; then
        return 0
    fi
    claude_mcp_state
    case $CLAUDE_STATE in same | stale) return 0 ;; esac
    return 1
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
        case $cs_output in
            *"Authorization: Bearer $KEY$NL"*"X-Relaydeck-Host: $MACHINE$NL"*) CLAUDE_STATE=same ;;
        esac
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
        --header "Authorization: Bearer $KEY" --header "X-Relaydeck-Host: $MACHINE" >"$LOG" 2>&1 </dev/null; then
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
#
# For the hosted server, the installer also installs the Relaydeck plugin, which has the inbox
# hooks. The hook script reads the key from the table. Codex runs the hooks of a plugin only after
# the person trusts them with /hooks.

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
            case $cx_output in
                *"\"Authorization\":\"Bearer$KEY\""*)
                    case $cx_output in *"\"X-Relaydeck-Host\":\"$MACHINE\""*) CODEX_STATE=same ;; esac
                    ;;
            esac
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
    printf '[mcp_servers.%s]\nurl = "%s"\nhttp_headers = { Authorization = "Bearer %s", X-Relaydeck-Host = "%s" }\n' \
        "$NAME" "$URL" "$KEY" "$MACHINE" >>"$WORK/out"
    commit_file "$ca_file" "$WORK/out"
}

codex_uses_plugin() { [ "$URL" = "$PLUGIN_URL" ] && [ "$NAME" = "$PLUGIN_NAME" ]; }

codex_has_plugin() { output_has "\"pluginId\":\"$PLUGIN_ID\"" codex plugin list --json; }

codex_connected() {
    codex_state
    case $CODEX_STATE in same | stale) return 0 ;; esac
    return 1
}

codex_install() {
    need_cli codex || return 0
    codex_table_install
    codex_plugin_install
}

codex_plugin_install() {
    codex_uses_plugin && [ "$R_STATE" != fail ] || return 0
    if codex_has_plugin; then
        # An earlier version of the plugin has no inbox hooks.
        if [ "$DRY_RUN" = 0 ]; then
            codex plugin marketplace upgrade "$PLUGIN_MARKETPLACE" >/dev/null 2>&1 </dev/null || true
            codex plugin add "$PLUGIN_ID" >/dev/null 2>&1 </dev/null || true
        fi
    elif [ "$DRY_RUN" = 1 ]; then
        if [ "$R_STATE" = same ]; then
            finish plan "would install the plugin $PLUGIN_ID for the inbox hooks"
        else
            R_TEXT="$R_TEXT, and install the plugin $PLUGIN_ID for the inbox hooks"
        fi
        return 0
    elif ! codex plugin marketplace add "$PLUGIN_REPO" >"$LOG" 2>&1 </dev/null ||
        ! codex plugin add "$PLUGIN_ID" >"$LOG" 2>&1 </dev/null; then
        add_warning "The plugin $PLUGIN_ID for the inbox hooks was not installed."
        return 0
    else
        add_note "Installed the plugin $PLUGIN_ID for the inbox hooks."
        R_STATE=changed
    fi
    CODEX_TRUST=1
}

codex_table_install() {
    ci_file=$(codex_config)
    ci_where=$(tilde "$ci_file")
    uses_file "$ci_file"
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
    settle_backup "$ci_file"
    finish changed "$ci_did $ci_where$(backup_note)"
}

codex_uninstall() {
    need_cli codex || return 0
    codex_table_uninstall
    codex_plugin_uninstall
}

codex_plugin_uninstall() {
    codex_uses_plugin && [ "$R_STATE" != fail ] || return 0
    cp_removed=''
    if codex_has_plugin; then
        if [ "$DRY_RUN" = 0 ] && ! codex plugin remove "$PLUGIN_ID" >"$LOG" 2>&1 </dev/null; then
            add_warning "Could not remove the plugin $PLUGIN_ID."
            return 0
        fi
        cp_removed="the plugin $PLUGIN_ID"
    fi
    if output_has "\"name\":\"$PLUGIN_MARKETPLACE\"" codex plugin marketplace list --json; then
        if [ "$DRY_RUN" = 0 ] && ! codex plugin marketplace remove "$PLUGIN_MARKETPLACE" >"$LOG" 2>&1 </dev/null; then
            add_warning "Could not remove the plugin marketplace $PLUGIN_MARKETPLACE."
            return 0
        fi
        cp_removed="${cp_removed:+$cp_removed and }the marketplace $PLUGIN_MARKETPLACE"
    fi
    [ -n "$cp_removed" ] || return 0
    case $DRY_RUN:$R_STATE in
        1:same) finish plan "would remove $cp_removed" ;;
        0:same) finish changed "removed $cp_removed" ;;
        *) R_TEXT="$R_TEXT, and $cp_removed" ;;
    esac
}

codex_table_uninstall() {
    cu_where=$(tilde "$(codex_config)")
    uses_file "$(codex_config)"
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

#
# Cursor starts a hook of the user in ~/.cursor, so the hook command has a relative path. Cursor
# has no hook that adds context to a prompt, thus the hooks are for the session start and the stop.

detect_cursor() { has_cmd cursor || has_cmd cursor-agent || [ -d "$HOME/.cursor" ]; }

cursor_connected() { json_connected "$HOME/.cursor/mcp.json" mcpServers url; }

# shellcheck disable=SC2016  # the shell of the hook reads $f, not this script
cursor_hook() {
    printf '{"command":"f=./hooks/%s; if [ -r \\"$f\\" ]; then sh \\"$f\\" cursor %s %s; fi","timeout":10}' \
        "$HOOK_FILE" "$1" "$NAME"
}

cursor_hooks() { HOOKS_JSON=$(printf '{"sessionStart":%s,"stop":%s}' "$(cursor_hook start)" "$(cursor_hook stop)"); }

cursor_install() {
    json_install "$HOME/.cursor/mcp.json" mcpServers url "$(printf '{"url":"%s",%s}' "$URL" "$(auth_header)")"
    cursor_hooks
    hooks_install "$HOME/.cursor" "$HOME/.cursor/hooks.json" hooks "{$NL  \"version\": 1$NL}"
}

cursor_uninstall() {
    json_uninstall "$HOME/.cursor/mcp.json" mcpServers url
    cursor_hooks
    hooks_uninstall "$HOME/.cursor" "$HOME/.cursor/hooks.json" hooks
}

# ---------------------------------------------------------------- Gemini CLI
#
# "gemini mcp add" accepts a header only as an argument, so the installer writes the settings file.
# The hooks are in the same file. Gemini CLI gives a hook timeout in milliseconds.

detect_gemini() { has_cmd gemini || [ -d "$HOME/.gemini" ]; }

gemini_connected() { json_connected "$HOME/.gemini/settings.json" mcpServers httpUrl; }

# shellcheck disable=SC2016  # the shell of the hook reads $HOME and $f, not this script
gemini_hook() {
    gh_start='f=\"$HOME/.gemini/hooks/'
    gh_end='\"; if [ -r \"$f\" ]; then sh \"$f\" gemini'
    printf '{"hooks":[{"name":"relaydeck-inbox","type":"command","command":"%s%s%s %s %s; fi","timeout":10000}]}' \
        "$gh_start" "$HOOK_FILE" "$gh_end" "$1" "$NAME"
}

gemini_hooks() {
    HOOKS_JSON=$(printf '{"SessionStart":%s,"BeforeAgent":%s,"AfterAgent":%s}' \
        "$(gemini_hook start)" "$(gemini_hook prompt)" "$(gemini_hook stop)")
}

gemini_install() {
    json_install "$HOME/.gemini/settings.json" mcpServers httpUrl \
        "$(printf '{"httpUrl":"%s",%s}' "$URL" "$(auth_header)")"
    gemini_hooks
    hooks_install "$HOME/.gemini" "$HOME/.gemini/settings.json" hooks '{}'
}

gemini_uninstall() {
    json_uninstall "$HOME/.gemini/settings.json" mcpServers httpUrl
    gemini_hooks
    hooks_uninstall "$HOME/.gemini" "$HOME/.gemini/settings.json" hooks
}

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

opencode_connected() { json_connected "$(opencode_config)" mcp url; }

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

zed_connected() { json_connected "$(zed_settings)" context_servers url; }

# ---------------------------------------------------------------- GitHub Copilot CLI
#
# VS Code reads the same file for its agent sessions.

copilot_config() { printf '%s\n' "${COPILOT_HOME:-$HOME/.copilot}/mcp-config.json"; }

detect_copilot() { has_cmd copilot || [ -d "$(dirname "$(copilot_config)")" ]; }

copilot_install() {
    json_install "$(copilot_config)" mcpServers url \
        "$(printf '{"type":"http","url":"%s",%s,"tools":["*"]}' "$URL" "$(auth_header)")"
}

copilot_uninstall() { json_uninstall "$(copilot_config)" mcpServers url; }

copilot_connected() { json_connected "$(copilot_config)" mcpServers url; }

# ---------------------------------------------------------------- Antigravity
#
# Antigravity keeps its files in the folder of Gemini CLI. It accepts only "serverUrl" for a remote server.

antigravity_config() { printf '%s\n' "$HOME/.gemini/config/mcp_config.json"; }

detect_antigravity() { has_cmd antigravity || [ -d "$HOME/.gemini/config" ] || [ -d "$HOME/.gemini/antigravity" ]; }

antigravity_install() {
    json_install "$(antigravity_config)" mcpServers serverUrl \
        "$(printf '{"serverUrl":"%s",%s}' "$URL" "$(auth_header)")"
}

antigravity_uninstall() { json_uninstall "$(antigravity_config)" mcpServers serverUrl; }

antigravity_connected() { json_connected "$(antigravity_config)" mcpServers serverUrl; }

# ---------------------------------------------------------------- Kiro

kiro_config() { printf '%s\n' "$HOME/.kiro/settings/mcp.json"; }

detect_kiro() { has_cmd kiro || has_cmd kiro-cli || [ -d "$HOME/.kiro" ]; }

kiro_install() {
    json_install "$(kiro_config)" mcpServers url "$(printf '{"url":"%s",%s}' "$URL" "$(auth_header)")"
}

kiro_uninstall() { json_uninstall "$(kiro_config)" mcpServers url; }

kiro_connected() { json_connected "$(kiro_config)" mcpServers url; }

# ---------------------------------------------------------------- Factory Droid
#
# "droid mcp add" accepts a header only as an argument, so the installer writes the file.

droid_config() { printf '%s\n' "$HOME/.factory/mcp.json"; }

detect_droid() { has_cmd droid || [ -d "$HOME/.factory" ]; }

droid_install() {
    json_install "$(droid_config)" mcpServers url \
        "$(printf '{"type":"http","url":"%s",%s,"disabled":false}' "$URL" "$(auth_header)")"
}

droid_uninstall() { json_uninstall "$(droid_config)" mcpServers url; }

droid_connected() { json_connected "$(droid_config)" mcpServers url; }

# ---------------------------------------------------------------- Cline
#
# Without "type", Cline uses the earlier SSE transport.

cline_config() {
    printf '%s\n' "${CLINE_MCP_SETTINGS_PATH:-$HOME/.cline/data/settings/cline_mcp_settings.json}"
}

detect_cline() { has_cmd cline || [ -d "$HOME/.cline" ]; }

cline_install() {
    json_install "$(cline_config)" mcpServers url \
        "$(printf '{"type":"streamableHttp","url":"%s",%s,"disabled":false,"autoApprove":[]}' "$URL" "$(auth_header)")"
}

cline_uninstall() { json_uninstall "$(cline_config)" mcpServers url; }

cline_connected() { json_connected "$(cline_config)" mcpServers url; }

# ---------------------------------------------------------------- Amp
#
# "amp mcp add" has no option for a header, so the installer writes the settings file.

amp_config() { printf '%s\n' "$HOME/.config/amp/settings.json"; }

detect_amp() { has_cmd amp || [ -d "$HOME/.config/amp" ]; }

amp_install() {
    json_install "$(amp_config)" amp.mcpServers url "$(printf '{"url":"%s",%s}' "$URL" "$(auth_header)")"
}

amp_uninstall() { json_uninstall "$(amp_config)" amp.mcpServers url; }

amp_connected() { json_connected "$(amp_config)" amp.mcpServers url; }

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
    CHANGED=0 FAILED=0 HOOKED='' CODEX_TRUST=0
    say ""
    for AGENT in $AGENTS; do
        : >"$LOG"
        WROTE='' USED='' R_WARNING=''
        finish fail "stopped before the end."
        "${AGENT}_${ACTION}" || true
        if [ "$DRY_RUN" = 0 ]; then
            printf '%s' "$USED" | while IFS= read -r ra_file; do
                tidy_backups "$ra_file"
            done
        fi
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

next_steps() {
    if [ -n "$HOOKED" ]; then
        say "Inbox hooks are on for: $HOOKED. These agents get your tasks, answers, and messages from the app without a prompt."
    fi
    if [ "$CODEX_TRUST" = 1 ]; then
        say "${YELLOW}Codex:${RESET} the inbox hooks are installed. Type /hooks in Codex one time and trust the Relaydeck hooks. Codex does not run them before that."
    fi
    say ""
    say "${BOLD}What to try next${RESET}"
    say "  1. Start a new session in each agent."
    say "  2. Say to the agent: Create a task named \"Hello\", and ask me a question on my phone."
    say "  3. Create a task for the agent in the Relaydeck app. The agent gets the task and starts the work."
}

summary() {
    say ""
    if [ "$DRY_RUN" = 1 ]; then
        say "Dry run: nothing was changed."
    elif [ "$ACTION" = uninstall ]; then
        say "Removed Relaydeck from $(agents_count "$CHANGED")."
    elif [ "$CHANGED" -gt 0 ]; then
        say "${BOLD}Done.${RESET} $(agents_count "$CHANGED") can now use the server \"$NAME\" at $URL."
    elif [ "$FAILED" = 0 ]; then
        say "Each agent was already set up."
    fi
    if [ "$ACTION" = install ]; then
        say "The app shows this machine as \"$MACHINE\". Use --host to give a different name."
        if [ "$DRY_RUN" = 0 ] && [ "$CHANGED" -gt 0 ]; then
            next_steps
        fi
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
    TUI=0
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

    USED=''
    if [ "$HAVE_TTY" = 1 ] && [ "$YES" = 0 ] && [ -z "$AGENTS_ARG" ]; then
        run_tui
        return
    fi
    say "${BOLD}Relaydeck installer${RESET}"
    say "Connects your coding agents to the Relaydeck app on iPhone, iPad, and Apple Watch."
    say "${DIM}Server: $URL${RESET}"
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

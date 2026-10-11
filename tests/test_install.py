"""End-to-end tests for install.sh.

Each test runs the real script in a temporary home directory. Stub commands replace the agent CLIs,
and PATH holds only those stubs and a few system tools, so no test can change real configuration.
"""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Parsed JSON has no static shape. The tests read it by key, so `Any` stays inside the test code.
type Json = dict[str, Any]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.sh"
HOOK_SCRIPT = ROOT / "plugins" / "relaydeck" / "hooks" / "inbox.sh"
MENU_PROMPT = "Press Enter to continue: "

GOOD_KEY = "mck_" + "A1b2C3d4" * 5
SECOND_KEY = "mck_" + "Z9y8X7w6" * 5
BAD_KEY = "mck_" + "0" * 40

SYSTEM_TOOLS = (
    "basename",
    "cat",
    "chmod",
    "cmp",
    "cp",
    "curl",
    "cut",
    "date",
    "dirname",
    "grep",
    "head",
    "mkdir",
    "mktemp",
    "mv",
    "readlink",
    "rm",
    "rmdir",
    "stty",
    "tail",
    "tr",
    "uname",
    "wc",
)

STUB_HEADER = """#!@PYTHON@
import json, os, sys
from pathlib import Path

STATE = Path(os.environ["STUB_STATE"])
ARGS = sys.argv[1:]
STDIN = sys.stdin.read()
FAIL = os.environ.get("STUB_FAIL", "").split(",")
with open(STATE / "calls.jsonl", "a") as log:
    record = {"tool": "@TOOL@", "argv": ARGS, "stdin": STDIN, "key_in_env": "RELAYDECK_API_KEY" in os.environ}
    log.write(json.dumps(record) + "\\n")
"""

# The answers have the same shape as the output of Claude Code 2.1.296.
CLAUDE_STUB = (
    STUB_HEADER
    + """
path = STATE / "claude.json"
state = {"marketplaces": [], "plugins": [], "key": None, "servers": {}}
if path.exists():
    state = json.loads(path.read_text())

def save():
    path.write_text(json.dumps(state))

def stop(message):
    print(message)
    sys.exit(1)

if ARGS[:3] == ["plugin", "list", "--json"]:
    listed = [{"id": plugin, "version": "1.0.0", "scope": "user", "enabled": True} for plugin in state["plugins"]]
    print(json.dumps(listed, indent=2))
elif ARGS[:4] == ["plugin", "marketplace", "list", "--json"]:
    listed = [{"name": "relaydeck", "source": "github", "repo": repo} for repo in state["marketplaces"]]
    print(json.dumps(listed, indent=2))
elif ARGS[:3] == ["plugin", "marketplace", "add"]:
    if "marketplace" in FAIL:
        stop("Failed to clone the marketplace repository")
    if ARGS[3] not in state["marketplaces"]:
        state["marketplaces"].append(ARGS[3])
    save()
    print("Successfully added marketplace: relaydeck")
elif ARGS[:3] == ["plugin", "marketplace", "remove"]:
    if not state["marketplaces"]:
        stop("Marketplace not found")
    state["marketplaces"] = []
    save()
elif ARGS[:3] == ["plugin", "marketplace", "update"] or ARGS[:2] == ["plugin", "update"]:
    print("Already at the latest version")
elif ARGS[:2] == ["plugin", "install"]:
    if not state["marketplaces"]:
        stop("Plugin not found in any marketplace")
    if ARGS[2] not in state["plugins"]:
        state["plugins"].append(ARGS[2])
    save()
    print("Successfully installed plugin: " + ARGS[2])
elif ARGS[:2] == ["plugin", "configure"] and "--values-stdin" in ARGS:
    if ARGS[2] not in state["plugins"]:
        stop("No installed plugin has the id")
    if "configure" in FAIL:
        stop("Failed to save configuration: Failed to save sensitive plugin options to secure storage")
    state["key"] = json.loads(STDIN)["api_key"]
    save()
    print("Configuration saved. Restart Claude Code to apply it.")
elif ARGS[:2] == ["plugin", "uninstall"]:
    if ARGS[2] not in state["plugins"]:
        stop("Plugin not found in installed plugins")
    state["plugins"].remove(ARGS[2])
    state["key"] = None
    save()
elif ARGS[:2] == ["mcp", "get"]:
    server = state["servers"].get(ARGS[2])
    if not server:
        stop('No MCP server named "%s".' % ARGS[2])
    print("%s:" % ARGS[2])
    print("  Scope: User config (available in all your projects)")
    print("  Status: Connected")
    print("  Type: http")
    print("  URL: %s" % server["url"])
    print("  Headers:")
    print("    %s" % server["header"])
    print("")
    print("To remove this server, run: claude mcp remove %s -s user" % ARGS[2])
elif ARGS[:2] == ["mcp", "add"]:
    words, options, rest = [], {}, ARGS[2:]
    while rest:
        if rest[0] in ("--transport", "--scope", "--header"):
            options[rest[0]] = rest[1]
            rest = rest[2:]
        else:
            words.append(rest[0])
            rest = rest[1:]
    if options.get("--transport") != "http" or options.get("--scope") != "user":
        stop("the stub accepts only an HTTP server in the user scope")
    if words[0] in state["servers"]:
        stop("MCP server %s already exists in user config" % words[0])
    state["servers"][words[0]] = {"url": words[1], "header": options["--header"]}
    save()
elif ARGS[:2] == ["mcp", "remove"]:
    if ARGS[2] not in state["servers"]:
        stop('No MCP server named "%s" in user scope' % ARGS[2])
    del state["servers"][ARGS[2]]
    save()
else:
    stop("claude stub: unknown command %r" % ARGS)
"""
)

# The answers have the same shape as the output of Codex CLI 0.162.0. The stub reads the real config.toml.
CODEX_STUB = (
    STUB_HEADER
    + """
import re, tomllib

config = Path(os.environ["CODEX_HOME"]) / "config.toml"

def load():
    try:
        return tomllib.loads(config.read_text()) if config.exists() else {}
    except tomllib.TOMLDecodeError:
        sys.exit("Error: failed to load bootstrap configuration")

def describe(name, server):
    headers = None if "reject" in FAIL else server.get("http_headers")
    return {
        "name": name,
        "enabled": True,
        "transport": {
            "type": "streamable_http",
            "url": server.get("url"),
            "bearer_token_env_var": server.get("bearer_token_env_var"),
            "http_headers": headers,
        },
    }

plugins_path = STATE / "codex-plugins.json"
plugins = {"marketplaces": [], "installed": []}
if plugins_path.exists():
    plugins = json.loads(plugins_path.read_text())

def save_plugins():
    plugins_path.write_text(json.dumps(plugins))

servers = load().get("mcp_servers", {})
in_config = set(servers)
if "plugin" in FAIL:
    servers.setdefault("tasks", {"url": os.environ["STUB_PLUGIN_URL"], "bearer_token_env_var": "TASKS_API_KEY"})
if ARGS[:3] == ["plugin", "list", "--json"]:
    installed = [{"pluginId": plugin, "installed": True, "enabled": True} for plugin in plugins["installed"]]
    print(json.dumps({"installed": installed, "available": []}, indent=2))
elif ARGS[:4] == ["plugin", "marketplace", "list", "--json"]:
    listed = [{"name": "relaydeck", "root": "/cache/" + source} for source in plugins["marketplaces"]]
    print(json.dumps({"marketplaces": listed}, indent=2))
elif ARGS[:3] == ["plugin", "marketplace", "add"]:
    if "marketplace" in FAIL:
        sys.exit("Error: failed to clone the marketplace")
    if ARGS[3] not in plugins["marketplaces"]:
        plugins["marketplaces"].append(ARGS[3])
    save_plugins()
    print("Added marketplace `relaydeck`")
elif ARGS[:3] == ["plugin", "marketplace", "upgrade"]:
    print("Upgraded marketplace `relaydeck`")
elif ARGS[:3] == ["plugin", "marketplace", "remove"]:
    plugins["marketplaces"] = []
    save_plugins()
elif ARGS[:2] == ["plugin", "add"]:
    if not plugins["marketplaces"]:
        sys.exit("Error: plugin not found")
    if ARGS[2] not in plugins["installed"]:
        plugins["installed"].append(ARGS[2])
    save_plugins()
    print("Added plugin `relaydeck` from marketplace `relaydeck`.")
elif ARGS[:2] == ["plugin", "remove"]:
    plugins["installed"].remove(ARGS[2])
    save_plugins()
elif ARGS[:2] == ["mcp", "get"]:
    if ARGS[2] not in servers:
        sys.exit("Error: No MCP server named '%s' found." % ARGS[2])
    print(json.dumps(describe(ARGS[2], servers[ARGS[2]]), indent=2))
elif ARGS[:2] == ["mcp", "list"]:
    print(json.dumps([describe(name, server) for name, server in servers.items()], indent=2))
elif ARGS[:2] == ["mcp", "remove"]:
    if ARGS[2] not in in_config:
        print("No MCP server named '%s' found." % ARGS[2])
        sys.exit(0)
    table = re.compile(r"^\\[mcp_servers\\.%s\\]\\n(?:(?!\\[).*\\n?)*" % re.escape(ARGS[2]), re.M)
    config.write_text(table.sub("", config.read_text()).rstrip("\\n") + "\\n")
else:
    sys.exit("codex stub: unknown command %r" % ARGS)
"""
)

# Stands in for curl when a test uses the hosted URL, so no test calls the live server.
CURL_STUB = (
    STUB_HEADER
    + """
accepted = os.environ["STUB_GOOD_KEYS"].split(",")
sys.stdout.write("200" if any('Bearer %s"' % key in STDIN for key in accepted) else "401")
"""
)

# A local stand-in for the REST API: GET /v1/tasks?limit=1 answers 200 for a known key, else 401.
KEY_SERVER = """
import http.server, json, sys

GOOD = set(sys.argv[2].split(","))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        authorization = self.headers.get("Authorization")
        with open(sys.argv[1], "a") as log:
            log.write(json.dumps([self.path, authorization]) + "\\n")
        good = authorization in GOOD and self.path == "/v1/tasks?limit=1"
        self.send_response(200 if good else 401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
"""

# Runs a command with a pseudo-terminal as its terminal, and types each reply after its prompt shows.
# It is a separate program because the test process can have threads, and a fork is not safe there.
TERMINAL_DRIVER = """
import json, os, pty, select, signal, sys, time

spec = json.loads(sys.argv[1])
pid, master = pty.fork()
if pid == 0:
    os.chdir(spec["cwd"])
    os.execve(spec["argv"][0], spec["argv"], spec["env"])
seen, window, steps = "", "", spec["steps"]
deadline = time.monotonic() + 240
while time.monotonic() < deadline:
    if steps and steps[0][0] in window:
        prompt, reply = steps.pop(0)
        window = window.split(prompt, 1)[1]
        os.write(master, reply.encode())
        continue
    if not select.select([master], [], [], 0.05)[0]:
        continue
    try:
        chunk = os.read(master, 4096)
    except OSError:
        break
    if not chunk:
        break
    seen += chunk.decode(errors="replace")
    window += chunk.decode(errors="replace")
else:
    os.killpg(pid, signal.SIGKILL)
status = os.waitpid(pid, 0)[1]
print(json.dumps({"code": os.waitstatus_to_exitcode(status), "terminal": seen, "unanswered": [s[0] for s in steps]}))
"""


@dataclass
class KeyServer:
    url: str
    log: Path

    @property
    def requests(self) -> list[list[str | None]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture(scope="session")
def key_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[KeyServer]:
    log = tmp_path_factory.mktemp("key-server") / "requests.jsonl"
    log.touch()
    command = [sys.executable, "-c", KEY_SERVER, str(log), f"Bearer {GOOD_KEY},Bearer {SECOND_KEY}"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:  # noqa: S603  the program is the text above
        assert process.stdout is not None
        try:
            yield KeyServer(f"http://127.0.0.1:{int(process.stdout.readline())}/mcp", log)
        finally:
            process.terminate()


@pytest.fixture(scope="session")
def toolbox(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Directories for PATH: the system tools, a python3, and a jq, each in its own directory."""
    base = tmp_path_factory.mktemp("toolbox")
    dirs = {name: base / name for name in ("system", "python", "jq")}
    for directory in dirs.values():
        directory.mkdir()
    for tool in SYSTEM_TOOLS:
        found = shutil.which(tool)
        assert found, f"{tool} is necessary for these tests"
        (dirs["system"] / tool).symlink_to(found)
    wrapper = dirs["python"] / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    if jq := shutil.which("jq"):
        (dirs["jq"] / "jq").symlink_to(jq)
    return dirs


def shells() -> list[object]:
    """sh, plus dash and bash when each is a different program from sh."""
    sh = shutil.which("sh") or "/bin/sh"
    probe = subprocess.run(  # noqa: S603  sh comes from PATH on the developer's machine
        [sh, "-c", "echo ${BASH_VERSION:+bash}"], capture_output=True, text=True, check=False
    )
    covered = {str(Path(sh).resolve())}
    if probe.stdout.strip():
        covered.add("bash")
    params: list[object] = [sh]
    for name in ("dash", "bash"):
        found = shutil.which(name)
        if found is None or str(Path(found).resolve()) in covered or name in covered:
            params.append(pytest.param(name, marks=pytest.mark.skip(reason=f"{name} is absent or the same as sh")))
        else:
            params.append(found)
    return params


@pytest.fixture(params=shells())
def shell(request: pytest.FixtureRequest) -> str:
    chosen: str = request.param
    return chosen


@dataclass
class Result:
    code: int
    out: str
    err: str = ""
    terminal: str = ""

    @property
    def text(self) -> str:
        return self.out + self.err + self.terminal


@dataclass
class Sandbox:
    root: Path
    shell: str
    toolbox: dict[str, Path]
    tools: list[str] = field(default_factory=lambda: ["python"])

    def __post_init__(self) -> None:
        for directory in (self.home, self.bin, self.state, self.codex_home, self.root / "tmp"):
            directory.mkdir()

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def codex_home(self) -> Path:
        return self.root / "codex"

    @property
    def env(self) -> dict[str, str]:
        path = [self.bin, *(self.toolbox[name] for name in self.tools), self.toolbox["system"]]
        return {
            "PATH": os.pathsep.join(str(entry) for entry in path),
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.codex_home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "TMPDIR": str(self.root / "tmp"),
            "STUB_STATE": str(self.state),
            "STUB_GOOD_KEYS": f"{GOOD_KEY},{SECOND_KEY}",
            "LANG": "C",
            "TERM": "xterm",
        }

    def stub(self, name: str, source: str) -> None:
        target = self.bin / name
        target.write_text(source.replace("@PYTHON@", sys.executable).replace("@TOOL@", name))
        target.chmod(0o755)

    def run(
        self, *args: str, key: str | None = None, env: dict[str, str] | None = None, script: str | None = None
    ) -> Result:
        """Runs the installer the way a pipe does: the script is stdin, and there is no terminal."""
        done = subprocess.run(  # noqa: S603  the shell under test runs the script of this repository
            [self.shell, "-s", "--", *args],
            input=SCRIPT.read_text() if script is None else script,
            capture_output=True,
            text=True,
            env=self.env | (env or {}) | ({"RELAYDECK_API_KEY": key} if key else {}),
            cwd=self.root,
            start_new_session=True,
            timeout=300,
            check=False,
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def run_in_terminal(self, *args: str, steps: list[tuple[str, str]], env: dict[str, str] | None = None) -> Result:
        """Runs `cat install.sh | sh` with a terminal, and types each reply after its prompt shows.

        stdout and stderr go to a file, so the terminal text holds only what the script sent to /dev/tty.
        """
        output = self.root / "output.txt"
        pipeline = 'script=$1; shell=$2; output=$3; shift 3; cat "$script" | "$shell" -s -- "$@" >"$output" 2>&1'
        argv = ["/bin/sh", "-c", pipeline, "sh", str(SCRIPT), self.shell, str(output), *args]
        code, terminal = self.drive_terminal(argv, steps, env or {})
        return Result(code, output.read_text(), terminal=terminal)

    def run_on_terminal(self, *args: str, env: dict[str, str] | None = None) -> str:
        """Runs the installer with a terminal as stdout, and gives the text that the terminal got."""
        return self.drive_terminal([self.shell, str(SCRIPT), *args], [], env or {})[1]

    def drive_terminal(self, argv: list[str], steps: list[tuple[str, str]], env: dict[str, str]) -> tuple[int, str]:
        spec = {"argv": argv, "env": self.env | env, "cwd": str(self.root), "steps": steps}
        done = subprocess.run(  # noqa: S603  the program is the text above
            [sys.executable, "-c", TERMINAL_DRIVER, json.dumps(spec)],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        report: Json = json.loads(done.stdout)
        assert report["unanswered"] == [], f"a prompt did not show. Terminal text: {report['terminal']!r}"
        return report["code"], report["terminal"]

    def calls(self, tool: str) -> list[Json]:
        log = self.state / "calls.jsonl"
        lines = log.read_text().splitlines() if log.exists() else []
        records: list[Json] = [json.loads(line) for line in lines]
        return [call for call in records if call["tool"] == tool]

    def commands(self, tool: str) -> list[str]:
        return [" ".join(call["argv"]) for call in self.calls(tool)]

    def claude_state(self) -> Json:
        state: Json = read_json(self.state / "claude.json")
        return state

    def files(self) -> set[str]:
        """Each file under the home and the Codex directory, as a relative path."""
        found: set[str] = set()
        for base in (self.home, self.codex_home):
            found |= {str(path.relative_to(self.root)) for path in base.rglob("*") if path.is_file()}
        return found

    def snapshot(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in self.files()}


@pytest.fixture
def box(tmp_path: Path, shell: str, toolbox: dict[str, Path]) -> Sandbox:
    return Sandbox(tmp_path, shell, toolbox)


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".relaydeck-backup-*"))


def entry(url: str, key: str = GOOD_KEY, url_field: str = "url") -> Json:
    return {url_field: url, "headers": {"Authorization": f"Bearer {key}"}}


def read_json(path: Path) -> Json:
    data: Json = json.loads(path.read_text())
    return data


def cursor_hooks(name: str = "tasks") -> Json:
    """The hooks that the installer adds to ~/.cursor/hooks.json."""

    def hook(event: str) -> Json:
        command = f'f=./hooks/relaydeck-inbox.sh; if [ -r "$f" ]; then sh "$f" cursor {event} {name}; fi'
        return {"command": command, "timeout": 10}

    return {"sessionStart": [hook("start")], "stop": [hook("stop")]}


def gemini_hooks(name: str = "tasks") -> Json:
    """The hooks that the installer adds to ~/.gemini/settings.json."""

    def group(event: str) -> list[Json]:
        command = f'f="$HOME/.gemini/hooks/relaydeck-inbox.sh"; if [ -r "$f" ]; then sh "$f" gemini {event} {name}; fi'
        return [{"hooks": [{"name": "relaydeck-inbox", "type": "command", "command": command, "timeout": 10000}]}]

    return {"SessionStart": group("start"), "BeforeAgent": group("prompt"), "AfterAgent": group("stop")}


# ------------------------------------------------------------------ options, platform, terminal


def test_help_lists_each_flag(box: Sandbox) -> None:
    result = box.run("--help")
    assert result.code == 0
    for flag in ("--agents", "--key", "--url", "--name", "--uninstall", "--dry-run", "--yes", "--help"):
        assert flag in result.out
    assert "RELAYDECK_API_KEY" in result.out
    listed = result.out.split("with commas between them:")[1].split("--key")[0]
    script = SCRIPT.read_text()
    agent_ids = script.split('AGENT_IDS="')[1].split('"')[0].split()
    assert [word.strip(",") for word in listed.split()] == agent_ids
    for agent in agent_ids:
        for function in ("detect", "install", "uninstall", "connected"):
            assert f"\n{function}_{agent}() " in script or f"\n{agent}_{function}() " in script


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--bogus"], "Unknown option: --bogus"),
        (["--agents"], "--agents needs a value"),
        (["--agents", "cursor,emacs"], "Unknown agent: emacs"),
        (["--agents", "cursor", "--url", "ftp://example.com"], "--url must be an http or https URL"),
        (["--agents", "cursor", "--url", 'https://example.com/"x'], "--url must be an http or https URL"),
        (["--agents", "cursor", "--name", "bad name"], "--name can have only"),
        (["--agents", "cursor", "--key", "not-a-key"], "An API key starts with mck_"),
    ],
)
def test_a_bad_option_is_a_usage_error(box: Sandbox, args: list[str], message: str) -> None:
    result = box.run(*args)
    assert result.code == 2
    assert message in result.err
    assert "Usage:" in result.err
    assert box.files() == set()


def test_no_terminal_needs_agents_and_a_key(box: Sandbox, key_server: KeyServer) -> None:
    without_agents = box.run("--url", key_server.url, key=GOOD_KEY)
    assert without_agents.code == 2
    assert "There is no terminal for questions. Give --agents." in without_agents.err

    without_key = box.run("--url", key_server.url, "--agents", "cursor")
    assert without_key.code == 2
    assert "Give --key, or set RELAYDECK_API_KEY." in without_key.err

    with_yes = box.run("--url", key_server.url, "--yes", key=GOOD_KEY)
    assert with_yes.code == 2
    assert box.files() == set()


def test_windows_gets_the_manual_steps(box: Sandbox) -> None:
    box.stub("uname", "#!/bin/sh\necho MINGW64_NT-10.0-19045\n")
    result = box.run("--agents", "cursor", key=GOOD_KEY)
    assert result.code == 1
    assert "does not support Windows" in result.out
    assert "https://github.com/Aayush9029/mcp-server#quick-start" in result.out
    assert box.files() == set()


def test_a_cut_download_does_nothing(box: Sandbox, key_server: KeyServer) -> None:
    script = SCRIPT.read_text()
    assert script.rstrip().endswith('main "$@"')
    box.stub("claude", CLAUDE_STUB)
    before = len(key_server.requests)
    for size in (len(script) // 5, len(script) // 2, len(script) - len('main "$@"\n'), len(script) - 4):
        cut = box.run("--agents", "claude,cursor", "--url", key_server.url, key=GOOD_KEY, script=script[:size])
        assert cut.out == ""
    assert box.files() == set()
    assert box.calls("claude") == []
    assert len(key_server.requests) == before


def test_colors_show_only_on_a_terminal(box: Sandbox) -> None:
    piped = box.run("--agents", "cursor", "--dry-run")
    assert "\x1b[" not in piped.text

    colored = box.run_on_terminal("--agents", "cursor", "--dry-run")
    assert "\x1b[1m" in colored
    plain = box.run_on_terminal("--agents", "cursor", "--dry-run", env={"NO_COLOR": "1"})
    assert "Relaydeck installer" in plain
    assert "\x1b[" not in plain


# ------------------------------------------------------------------ detection and selection


def test_the_menu_lists_only_the_agents_that_are_found(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("claude", CLAUDE_STUB)
    box.stub("zed", "#!/bin/sh\n")
    (box.home / ".cursor").mkdir()
    (box.home / ".t3").mkdir()
    result = box.run_in_terminal("--url", key_server.url, "--dry-run", steps=[(MENU_PROMPT, "\n")])
    assert result.code == 0
    menu = result.terminal
    assert "[x]  1) Claude Code" in menu
    assert "[x]  2) Cursor" in menu
    assert "[x]  3) Zed" in menu
    assert " 4) " not in menu
    missing = "Codex, Gemini CLI, opencode, GitHub Copilot CLI, Antigravity, Kiro, Factory Droid, Cline, Amp"
    assert f"Not found: {missing}. Use --agents to connect one of them." in menu
    assert "T3 Code uses the agents in this list" in menu
    assert "connected" not in menu
    for label in ("Claude Code", "Cursor", "Zed"):
        assert label in result.out
    for label in ("Codex", "Gemini CLI", "opencode", "Kiro"):
        assert label not in result.out


def test_the_menu_marks_an_agent_that_is_connected(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("claude", CLAUDE_STUB)
    box.stub("codex", CODEX_STUB)
    (box.home / ".gemini").mkdir()
    assert box.run("--url", key_server.url, "--agents", "codex,cursor", key=GOOD_KEY).code == 0

    result = box.run_in_terminal("--url", key_server.url, "--dry-run", steps=[(MENU_PROMPT, "\n")])
    rows = {line.split(") ")[1].split("  ")[0]: line for line in result.terminal.splitlines() if ") " in line}
    assert list(rows) == ["Claude Code", "Codex", "Cursor", "Gemini CLI"]
    assert [name for name, row in rows.items() if "+ connected" in row] == ["Codex", "Cursor"]
    assert all(row.strip().startswith("[x]") for row in rows.values())
    assert "T3 Code" not in result.terminal

    removal = box.run_in_terminal("--url", key_server.url, "--uninstall", "--dry-run", steps=[(MENU_PROMPT, "\n")])
    rows = {line.split(") ")[1].split("  ")[0]: line for line in removal.terminal.splitlines() if ") " in line}
    assert [name for name, row in rows.items() if row.strip().startswith("[x]")] == ["Codex", "Cursor"]
    assert removal.out.count("would remove") == 2


def test_the_menu_switches_agents_on_and_off(box: Sandbox, key_server: KeyServer) -> None:
    for folder in (".cursor", ".gemini", ".kiro"):
        (box.home / folder).mkdir()
    steps = [(MENU_PROMPT, "1, 3 9 x\n"), (MENU_PROMPT, "n\n"), (MENU_PROMPT, "2\n"), (MENU_PROMPT, "\n")]
    result = box.run_in_terminal("--url", key_server.url, "--dry-run", steps=steps)
    assert result.code == 0
    assert '"9" is not a number in the list' in result.terminal
    assert '"x" is not a number in the list' in result.terminal
    assert "[ ]  1) Cursor" in result.terminal
    assert "[ ]  3) Kiro" in result.terminal
    assert "Gemini CLI" in result.out
    assert "Cursor" not in result.out
    assert "Kiro" not in result.out


def test_no_selection_changes_nothing(box: Sandbox, key_server: KeyServer) -> None:
    (box.home / ".cursor").mkdir()
    result = box.run_in_terminal("--url", key_server.url, steps=[(MENU_PROMPT, "n\n"), (MENU_PROMPT, "\n")])
    assert result.code == 0
    assert "No agent is selected. Nothing was changed." in result.out
    assert box.files() == set()


def test_a_machine_without_agents_gets_the_manual_steps(box: Sandbox, key_server: KeyServer) -> None:
    result = box.run_in_terminal("--url", key_server.url, steps=[])
    assert result.code == 1
    assert "No supported agent was found. Use --agents to choose one." in result.out
    assert "https://github.com/Aayush9029/mcp-server#quick-start" in result.out
    assert "Press Enter" not in result.terminal
    assert box.files() == set()


def test_the_agent_list_accepts_names_in_any_case(box: Sandbox, key_server: KeyServer) -> None:
    names = "Cursor, GEMINI-CLI,cursor,Copilot-CLI,kiro-cli,FACTORY"
    result = box.run("--url", key_server.url, "--dry-run", "--agents", names)
    assert result.code == 0
    lines = [line for line in result.out.splitlines() if "would" in line]
    labels = ["Cursor", "Gemini CLI", "GitHub Copilot CLI", "Kiro", "Factory Droid"]
    assert [line.split("  ")[1].split(" ", 1)[1].strip() for line in lines] == labels


def test_yes_uses_the_agents_that_are_found(box: Sandbox, key_server: KeyServer) -> None:
    (box.home / ".cursor").mkdir()
    result = box.run_in_terminal("--url", key_server.url, "--dry-run", "--yes", steps=[])
    assert result.code == 0
    assert "would add" in result.out
    assert "Press Enter" not in result.terminal


# ------------------------------------------------------------------ the API key


def test_a_good_key_is_checked_and_stays_secret(box: Sandbox, key_server: KeyServer) -> None:
    before = len(key_server.requests)
    result = box.run("--url", key_server.url, "--agents", "cursor", key=GOOD_KEY)
    assert result.code == 0, result.text
    assert key_server.requests[before:] == [["/v1/tasks?limit=1", f"Bearer {GOOD_KEY}"]]
    assert "The server accepted the API key." in result.out
    assert GOOD_KEY not in result.text
    assert read_json(box.home / ".cursor/mcp.json") == {"mcpServers": {"tasks": entry(key_server.url)}}


def test_a_bad_key_stops_the_installer(box: Sandbox, key_server: KeyServer) -> None:
    result = box.run("--url", key_server.url, "--agents", "cursor", key=BAD_KEY)
    assert result.code == 1
    assert "The server did not accept this API key. Nothing was changed." in result.err
    assert BAD_KEY not in result.text
    assert box.files() == set()


def test_a_server_that_does_not_answer_stops_the_installer(box: Sandbox) -> None:
    result = box.run("--url", "http://127.0.0.1:9/mcp", "--agents", "cursor", key=GOOD_KEY)
    assert result.code == 1
    assert "Could not check the API key (HTTP status 000" in result.err
    assert box.files() == set()


def test_the_key_flag_works(box: Sandbox, key_server: KeyServer) -> None:
    result = box.run("--url", key_server.url, "--agents", "cursor", f"--key={GOOD_KEY}")
    assert result.code == 0, result.text
    assert GOOD_KEY in (box.home / ".cursor/mcp.json").read_text()


def test_the_key_prompt_hides_the_key_and_asks_again(box: Sandbox, key_server: KeyServer) -> None:
    prompt = "API key (the text stays hidden): "
    result = box.run_in_terminal(
        "--url",
        key_server.url,
        "--agents",
        "cursor",
        steps=[(prompt, "hello\n"), (prompt, BAD_KEY + "\n"), (prompt, f"  {GOOD_KEY} \n")],
    )
    assert result.code == 0, result.text
    assert "That is not an API key." in result.terminal
    assert "The server did not accept this API key." in result.terminal
    for secret in ("hello", BAD_KEY, GOOD_KEY):
        assert secret not in result.text
    assert read_json(box.home / ".cursor/mcp.json")["mcpServers"]["tasks"] == entry(key_server.url)


def test_three_bad_keys_stop_the_installer(box: Sandbox, key_server: KeyServer) -> None:
    prompt = "API key (the text stays hidden): "
    result = box.run_in_terminal(
        "--url", key_server.url, "--agents", "cursor", steps=[(prompt, "a\n"), (prompt, "b\n"), (prompt, "c\n")]
    )
    assert result.code == 1
    assert "No valid API key after 3 tries" in result.out
    assert box.files() == set()


# ------------------------------------------------------------------ JSON files (Cursor, Gemini CLI, opencode, Zed)


def test_each_json_agent_gets_its_own_format(box: Sandbox, key_server: KeyServer) -> None:
    agents = "cursor,gemini,opencode,zed,copilot,antigravity,kiro,droid,cline,amp"
    result = box.run("--url", key_server.url, "--agents", agents, key=GOOD_KEY)
    assert result.code == 0, result.text
    url = key_server.url
    headers = {"Authorization": f"Bearer {GOOD_KEY}"}
    remote: Json = {"url": url, "headers": headers}
    cline: Json = {"type": "streamableHttp", **remote, "disabled": False, "autoApprove": []}
    expected: dict[str, Json] = {
        ".cursor/mcp.json": {"mcpServers": {"tasks": remote}},
        ".gemini/settings.json": {
            "mcpServers": {"tasks": {"httpUrl": url, "headers": headers}},
            "hooks": gemini_hooks(),
        },
        ".config/opencode/opencode.json": {
            "mcp": {"tasks": {"type": "remote", "url": url, "enabled": True, "oauth": False, "headers": headers}}
        },
        ".config/zed/settings.json": {"context_servers": {"tasks": remote}},
        ".copilot/mcp-config.json": {"mcpServers": {"tasks": {"type": "http", **remote, "tools": ["*"]}}},
        ".gemini/config/mcp_config.json": {"mcpServers": {"tasks": {"serverUrl": url, "headers": headers}}},
        ".kiro/settings/mcp.json": {"mcpServers": {"tasks": remote}},
        ".factory/mcp.json": {"mcpServers": {"tasks": {"type": "http", **remote, "disabled": False}}},
        ".cline/data/settings/cline_mcp_settings.json": {"mcpServers": {"tasks": cline}},
        ".config/amp/settings.json": {"amp.mcpServers": {"tasks": remote}},
    }
    for name, content in expected.items():
        path = box.home / name
        assert read_json(path) == content
        assert path.stat().st_mode & 0o777 == 0o600
    hook_files = [".cursor/hooks.json", ".cursor/hooks/relaydeck-inbox.sh", ".gemini/hooks/relaydeck-inbox.sh"]
    assert box.files() == {f"home/{name}" for name in [*expected, *hook_files]}
    assert "Done. 10 agents can now use the server" in result.out

    again = box.run("--url", key_server.url, "--agents", agents, key=GOOD_KEY)
    assert again.out.count("No change.") == 10
    removed = box.run("--url", key_server.url, "--agents", agents, "--uninstall")
    assert removed.code == 0, removed.text
    assert "Removed Relaydeck from 10 agents." in removed.out
    assert not any(GOOD_KEY.encode() in content for content in box.snapshot().values())
    assert read_json(box.home / ".config/amp/settings.json") == {"amp.mcpServers": {}}


def test_an_agent_file_can_be_in_a_different_place(box: Sandbox, key_server: KeyServer) -> None:
    env = {"COPILOT_HOME": str(box.home / "copilot"), "CLINE_MCP_SETTINGS_PATH": str(box.home / "cline/mcp.json")}
    result = box.run("--url", key_server.url, "--agents", "copilot,cline", key=GOOD_KEY, env=env)
    assert result.code == 0, result.text
    assert box.files() == {"home/copilot/mcp-config.json", "home/cline/mcp.json"}


CURSOR_FILE = """{
    "mcpServers": {
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"TOKEN": "caf\\u00e9 ☕"}
        },
        "linear": {"url": "https://mcp.linear.app/mcp"}
    },
    "other": [1, 2.50, true, null]
}
"""


def test_a_merge_keeps_the_other_servers(box: Sandbox, key_server: KeyServer) -> None:
    path = box.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text(CURSOR_FILE)
    path.chmod(0o644)
    result = box.run("--url", key_server.url, "--agents", "cursor", key=GOOD_KEY)
    assert result.code == 0, result.text

    text = path.read_text()
    block = (
        '        "tasks": {\n'
        f'            "url": "{key_server.url}",\n'
        '            "headers": {\n'
        f'                "Authorization": "Bearer {GOOD_KEY}"\n'
        "            }\n"
        "        },\n"
    )
    start = '{\n    "mcpServers": {\n'
    assert text == start + block + CURSOR_FILE[len(start) :]
    assert path.stat().st_mode & 0o777 == 0o600

    saved = backups(path)
    assert len(saved) == 1
    assert saved[0].read_text() == CURSOR_FILE
    assert f"(backup: {saved[0].name})" in result.out
    assert not list(path.parent.glob(".relaydeck-install.*"))


ZED_FILE = """// Zed settings
{
  "theme": "One Dark", // the theme
  /* servers */
  "context_servers": {
    // my own server
    "local": { "command": "node", "args": ["server.js"], },
  },
  "tab_size": 2,
}
"""


def test_comments_and_trailing_commas_stay(box: Sandbox, key_server: KeyServer) -> None:
    path = box.home / ".config/zed/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(ZED_FILE)
    installed = box.run("--url", key_server.url, "--agents", "zed", key=GOOD_KEY)
    assert installed.code == 0, installed.text
    text = path.read_text()
    assert f'"Authorization": "Bearer {GOOD_KEY}"' in text
    for line in ZED_FILE.splitlines():
        assert line in text
    assert text.index('"tasks"') < text.index("// my own server")

    removed = box.run("--url", key_server.url, "--agents", "zed", "--uninstall")
    assert removed.code == 0, removed.text
    assert path.read_text() == ZED_FILE


@pytest.mark.parametrize(
    "original",
    [
        "{}",
        "{}\n",
        '{"mcpServers": {}}',
        '{"mcpServers": {"a": {"url": "https://a.example"}}}',
        '{\n\t"mcpServers": {\n\t\t"a": {"url": "https://a.example"}\n\t}\n}\n',
        '{\r\n  "theme": "x",\r\n  "mcpServers": {\r\n    "a": {}\r\n  }\r\n}\r\n',
        '{\n  "mcpServers": {\n    // none yet\n  }\n}\n',
        '{"z": {"mcpServers": 1}, "mcpServers": {"b": [1, {"tasks": 2}]}}',
    ],
)
def test_install_then_uninstall_gives_the_first_data_again(box: Sandbox, key_server: KeyServer, original: str) -> None:
    path = box.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_bytes(original.encode())
    installed = box.run("--url", key_server.url, "--agents", "cursor", key=GOOD_KEY)
    assert installed.code == 0, installed.text
    assert _loose_json(path)["mcpServers"]["tasks"] == entry(key_server.url)
    if "\r\n" in original:
        assert "\n" not in path.read_bytes().decode().replace("\r\n", "")

    removed = box.run("--url", key_server.url, "--agents", "cursor", "--uninstall")
    assert removed.code == 0, removed.text
    assert _loose_json(path) == _loose_json(path, original)
    if original.strip() not in ("{}", '{"mcpServers": {}}'):
        assert path.read_bytes().decode() == original


def _loose_json(path: Path, text: str | None = None) -> Json:
    """Reads JSON that can have // comments on their own lines."""
    lines = (text if text is not None else path.read_bytes().decode()).splitlines()
    data: Json = json.loads("\n".join(line for line in lines if not line.strip().startswith("//")))
    data.setdefault("mcpServers", {})
    return data


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ('{"mcpServers": {"a": 1,}', "is not valid JSON (line 1:"),
        ('{\n  "mcpServers": {\n    "a" 1\n  }\n}\n', 'is not valid JSON (line 3: a ":" must be here)'),
        ("[1, 2]\n", "the top level is not an object"),
        ('{"mcpServers": []}\n', '"mcpServers" is not an object'),
        ('{"a": 1} trailing\n', "there is text after the end of the data"),
        ('{"a": /* open', "a comment has no end"),
    ],
)
def test_a_malformed_file_is_not_changed(box: Sandbox, key_server: KeyServer, content: str, reason: str) -> None:
    path = box.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text(content)
    for args in (["--agents", "cursor,gemini"], ["--agents", "cursor,gemini", "--uninstall"]):
        result = box.run("--url", key_server.url, *args, key=GOOD_KEY)
        assert result.code == 1
        assert reason in result.out
        assert "Not changed." in result.out
        assert "Not complete for 1 agent." in result.out
        assert path.read_text() == content
        assert backups(path) == []
        assert not (path.parent / "hooks.json").exists()
        gemini = next(line for line in result.out.splitlines() if "Gemini CLI" in line)
        assert "~/.gemini/settings.json" in gemini


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read each file")
def test_an_unreadable_file_is_not_changed(box: Sandbox, key_server: KeyServer) -> None:
    path = box.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text("{}")
    path.chmod(0o000)
    result = box.run("--url", key_server.url, "--agents", "cursor", key=GOOD_KEY)
    assert result.code == 1
    assert "cannot read ~/.cursor/mcp.json. Not changed." in result.out
    path.chmod(0o600)
    assert path.read_text() == "{}"
    assert backups(path) == []


def test_a_symbolic_link_stays_a_link(box: Sandbox, key_server: KeyServer) -> None:
    real = box.root / "dotfiles/cursor-mcp.json"
    real.parent.mkdir()
    real.write_text('{"mcpServers": {}}\n')
    link = box.home / ".cursor/mcp.json"
    link.parent.mkdir()
    link.symlink_to("../../dotfiles/cursor-mcp.json")
    result = box.run("--url", key_server.url, "--agents", "cursor", key=GOOD_KEY)
    assert result.code == 0, result.text
    assert link.is_symlink()
    assert read_json(real)["mcpServers"]["tasks"] == entry(key_server.url)
    assert len(backups(real)) == 1


def test_opencode_uses_the_jsonc_file_when_it_is_the_only_one(box: Sandbox, key_server: KeyServer) -> None:
    path = box.home / ".config/opencode/opencode.jsonc"
    path.parent.mkdir(parents=True)
    path.write_text('{\n  // my config\n  "theme": "system"\n}\n')
    result = box.run("--url", key_server.url, "--agents", "opencode", key=GOOD_KEY)
    assert result.code == 0, result.text
    assert "// my config" in path.read_text()
    assert f"Bearer {GOOD_KEY}" in path.read_text()
    assert not (path.parent / "opencode.json").exists()


# ------------------------------------------------------------------ inbox hooks (Cursor, Gemini CLI)


def test_cursor_and_gemini_get_the_inbox_hooks(box: Sandbox, key_server: KeyServer) -> None:
    args = ["--url", key_server.url, "--agents", "cursor,gemini"]
    planned = box.run(*args, "--dry-run")
    assert 'would add "tasks" to ~/.cursor/mcp.json, with the inbox hooks in ~/.cursor/hooks.json' in planned.out
    assert box.files() == set()

    result = box.run(*args, key=GOOD_KEY)
    assert result.code == 0, result.text
    assert 'added "tasks" to ~/.cursor/mcp.json. Added the inbox hooks to ~/.cursor/hooks.json.' in result.out
    assert "Inbox hooks are on for: Cursor, Gemini CLI." in result.out
    assert "What to try next" in result.out
    assert "Create a task for the agent in the Relaydeck app." in result.out
    assert "trust the Relaydeck hooks" not in result.out
    assert read_json(box.home / ".cursor/hooks.json") == {"version": 1, "hooks": cursor_hooks()}
    assert (box.home / ".cursor/hooks.json").read_text().startswith('{\n  "hooks": {\n    "stop": [\n')
    assert read_json(box.home / ".gemini/settings.json")["hooks"] == gemini_hooks()
    scripts = [box.home / folder / "hooks/relaydeck-inbox.sh" for folder in (".cursor", ".gemini")]
    for script in scripts:
        assert script.read_text() == HOOK_SCRIPT.read_text()
        assert script.stat().st_mode & 0o777 == 0o600
    assert backups(box.home / ".gemini/settings.json") == []

    snapshot = box.snapshot()
    again = box.run(*args, key=GOOD_KEY)
    assert again.out.count("No change.") == 2
    assert box.snapshot() == snapshot

    planned = box.run(*args, "--uninstall", "--dry-run")
    assert planned.out.count("and the inbox hooks from") == 2
    assert box.snapshot() == snapshot
    removed = box.run(*args, "--uninstall")
    assert removed.code == 0, removed.text
    assert 'removed "tasks" from ~/.cursor/mcp.json and the inbox hooks from ~/.cursor/hooks.json' in removed.out
    assert read_json(box.home / ".cursor/hooks.json") == {"version": 1}
    assert read_json(box.home / ".gemini/settings.json") == {"mcpServers": {}}
    assert not any(script.exists() or script.parent.exists() for script in scripts)
    assert box.run(*args, "--uninstall").out.count("Nothing to remove.") == 2


CURSOR_HOOKS_FILE = """{
  "version": 1,
  "hooks": {
    // format each file after an edit
    "afterFileEdit": [{ "command": "./hooks/format.sh" }],
    "stop": [
      { "command": "./hooks/audit.sh", "loop_limit": 10 }
    ]
  }
}
"""
GEMINI_HOOKS_FILE = """{
  "theme": "dark",
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "echo hello" }]
      }
    ]
  }
}
"""


def test_the_hooks_of_the_person_stay(box: Sandbox, key_server: KeyServer) -> None:
    cursor = box.home / ".cursor/hooks.json"
    gemini = box.home / ".gemini/settings.json"
    for path, content in ((cursor, CURSOR_HOOKS_FILE), (gemini, GEMINI_HOOKS_FILE)):
        path.parent.mkdir()
        path.write_text(content)
    (box.home / ".cursor/hooks").mkdir()
    (box.home / ".cursor/hooks/format.sh").write_text("#!/bin/sh\n")
    args = ["--url", key_server.url, "--agents", "cursor,gemini"]
    result = box.run(*args, key=GOOD_KEY)
    assert result.code == 0, result.text

    audit: Json = {"command": "./hooks/audit.sh", "loop_limit": 10}
    assert "// format each file after an edit" in cursor.read_text()
    hooks = _loose_json(cursor)["hooks"]
    assert hooks["afterFileEdit"] == [{"command": "./hooks/format.sh"}]
    assert hooks["stop"] == [cursor_hooks()["stop"][0], audit]
    assert hooks["sessionStart"] == cursor_hooks()["sessionStart"]
    assert [backup.read_text() for backup in backups(cursor)] == [CURSOR_HOOKS_FILE]
    hello: Json = {"hooks": [{"type": "command", "command": "echo hello"}]}
    settings = read_json(gemini)
    assert settings["hooks"] == gemini_hooks() | {"SessionStart": [gemini_hooks()["SessionStart"][0], hello]}
    assert [backup.read_text() for backup in backups(gemini)] == [GEMINI_HOOKS_FILE]

    # A different server name gives new hook commands. The agent keeps one set of Relaydeck hooks.
    renamed = box.run(*args, "--name", "relay", key=GOOD_KEY)
    assert renamed.code == 0, renamed.text
    assert _loose_json(cursor)["hooks"]["stop"] == [cursor_hooks("relay")["stop"][0], audit]
    assert read_json(gemini)["hooks"]["AfterAgent"] == gemini_hooks("relay")["AfterAgent"]

    assert box.run(*args, "--name", "relay", "--uninstall").code == 0
    assert box.run(*args, "--uninstall").code == 0
    assert cursor.read_text() == CURSOR_HOOKS_FILE
    assert read_json(gemini) == json.loads(GEMINI_HOOKS_FILE) | {"mcpServers": {}}
    assert (box.home / ".cursor/hooks/format.sh").exists()
    assert not (box.home / ".cursor/hooks/relaydeck-inbox.sh").exists()


def test_a_hook_file_that_is_not_valid_keeps_the_server(box: Sandbox, key_server: KeyServer) -> None:
    path = box.home / ".cursor/hooks.json"
    path.parent.mkdir()
    path.write_text('{"version": 1, "hooks": []}\n')
    result = box.run("--url", key_server.url, "--agents", "cursor", key=GOOD_KEY)
    assert result.code == 0, result.text
    assert "The inbox hooks were not changed: ~/.cursor/hooks.json is not valid JSON for the installer." in result.out
    assert "Inbox hooks are on for" not in result.out
    assert path.read_text() == '{"version": 1, "hooks": []}\n'
    assert read_json(box.home / ".cursor/mcp.json") == {"mcpServers": {"tasks": entry(key_server.url)}}
    assert not (box.home / ".cursor/hooks").exists()


# ------------------------------------------------------------------ a second run, a new key, a name conflict


def test_a_second_run_changes_nothing(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("claude", CLAUDE_STUB)
    box.stub("codex", CODEX_STUB)
    (box.codex_home / "config.toml").write_text('model = "gpt"\n')
    args = ["--url", key_server.url, "--agents", "claude,codex,cursor,gemini,opencode,zed"]
    first = box.run(*args, key=GOOD_KEY)
    assert first.code == 0, first.text
    assert "Done. 6 agents can now use the server" in first.out
    snapshot = box.snapshot()
    claude_state = box.claude_state()
    claude_calls, codex_calls = box.commands("claude"), box.commands("codex")

    second = box.run(*args, key=GOOD_KEY)
    assert second.code == 0, second.text
    assert second.out.count("No change.") == 6
    assert "Each agent was already set up." in second.out
    assert box.snapshot() == snapshot
    assert box.claude_state() == claude_state
    assert box.commands("claude")[len(claude_calls) :] == ["mcp get tasks"]
    assert box.commands("codex")[len(codex_calls) :] == ["mcp get tasks --json"]


def test_a_new_key_replaces_the_old_key(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("codex", CODEX_STUB)
    args = ["--url", key_server.url, "--agents", "codex,cursor"]
    assert box.run(*args, key=GOOD_KEY).code == 0
    result = box.run(*args, key=SECOND_KEY)
    assert result.code == 0, result.text
    assert result.out.count("updated") == 2

    cursor = box.home / ".cursor/mcp.json"
    assert read_json(cursor) == {"mcpServers": {"tasks": entry(key_server.url, SECOND_KEY)}}
    assert len(backups(cursor)) == 1
    config = box.codex_home / "config.toml"
    servers = tomllib.loads(config.read_text())["mcp_servers"]
    assert servers == {"tasks": {"url": key_server.url, "http_headers": {"Authorization": f"Bearer {SECOND_KEY}"}}}
    assert len(backups(config)) == 1
    assert GOOD_KEY in backups(config)[0].read_text()


FOREIGN = '{\n  "mcpServers": {\n    "tasks": {\n      "url": "https://other.example/mcp"\n    }\n  }\n}\n'


def test_a_different_server_with_the_same_name_is_not_replaced(box: Sandbox, key_server: KeyServer) -> None:
    path = box.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text(FOREIGN)
    for extra in ([], ["--yes"]):
        result = box.run("--url", key_server.url, "--agents", "cursor", *extra, key=GOOD_KEY)
        assert result.code == 1
        assert 'a different server named "tasks" is in ~/.cursor/mcp.json. Not changed.' in result.out
        assert "--name" in result.out
        assert path.read_text() == FOREIGN

    removed = box.run("--url", key_server.url, "--agents", "cursor", "--uninstall")
    assert removed.code == 0
    assert "has a different URL. Left unchanged." in removed.out
    assert path.read_text() == FOREIGN

    named = box.run("--url", key_server.url, "--agents", "cursor", "--name", "relaydeck", key=GOOD_KEY)
    assert named.code == 0, named.text
    servers = read_json(path)["mcpServers"]
    assert servers == {"relaydeck": entry(key_server.url), "tasks": {"url": "https://other.example/mcp"}}


@pytest.mark.parametrize(("answer", "replaced"), [("y\n", True), ("\n", False)])
def test_the_installer_asks_before_it_replaces_a_server(
    box: Sandbox, key_server: KeyServer, answer: str, replaced: bool
) -> None:
    path = box.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text(FOREIGN)
    steps = [("Replace it? [y/N] ", answer)]
    env = {"RELAYDECK_API_KEY": GOOD_KEY}
    result = box.run_in_terminal("--url", key_server.url, "--agents", "cursor", steps=steps, env=env)
    assert result.code == (0 if replaced else 1)
    if replaced:
        assert read_json(path) == {"mcpServers": {"tasks": entry(key_server.url)}}
        assert backups(path)[0].read_text() == FOREIGN
    else:
        assert path.read_text() == FOREIGN


# ------------------------------------------------------------------ uninstall and dry run


def test_uninstall_removes_only_the_relaydeck_entry(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("claude", CLAUDE_STUB)
    box.stub("codex", CODEX_STUB)
    cursor = box.home / ".cursor/mcp.json"
    cursor.parent.mkdir()
    cursor.write_text(CURSOR_FILE)
    config = box.codex_home / "config.toml"
    toml = 'model = "gpt"\n\n[mcp_servers.docs]\nurl = "https://docs.example/mcp"\n'
    config.write_text(toml)
    agents = ["--url", key_server.url, "--agents", "claude,codex,cursor,gemini"]
    assert box.run(*agents, key=GOOD_KEY).code == 0

    result = box.run(*agents, "--uninstall")
    assert result.code == 0, result.text
    assert "Removed Relaydeck from 4 agents." in result.out
    assert cursor.read_text() == CURSOR_FILE
    assert config.read_text() == toml
    assert read_json(box.home / ".gemini/settings.json") == {"mcpServers": {}}
    assert box.claude_state()["servers"] == {}
    assert [backup.read_text() for backup in backups(cursor)] == [CURSOR_FILE]
    assert [backup.read_text() for backup in backups(config)] == [toml]
    assert not any(GOOD_KEY.encode() in content for content in box.snapshot().values())
    assert GOOD_KEY not in result.text

    again = box.run(*agents, "--uninstall")
    assert again.code == 0
    assert again.out.count("Nothing to remove.") == 4
    assert "Removed Relaydeck from 0 agents." in again.out
    assert len(backups(cursor)) == 1


def test_a_dry_run_changes_nothing(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("claude", CLAUDE_STUB)
    box.stub("codex", CODEX_STUB)
    cursor = box.home / ".cursor/mcp.json"
    cursor.parent.mkdir()
    cursor.write_text(CURSOR_FILE)
    before_files, before_requests = box.files(), len(key_server.requests)
    agents = ["--url", key_server.url, "--agents", "claude,codex,cursor,zed"]

    result = box.run(*agents, "--dry-run")
    assert result.code == 0, result.text
    assert result.out.count("would ") == 4
    assert 'would add "tasks" to ~/.cursor/mcp.json' in result.out
    assert "Dry run: nothing was changed." in result.out
    assert box.files() == before_files
    assert cursor.read_text() == CURSOR_FILE
    assert len(key_server.requests) == before_requests
    assert box.commands("claude") == ["mcp get tasks"]
    assert box.commands("codex") == ["mcp get tasks --json", "mcp list --json"]
    assert "would write [mcp_servers.tasks]" in result.out

    assert box.run(*agents, key=GOOD_KEY).code == 0
    installed = box.snapshot()
    removal = box.run(*agents, "--uninstall", "--dry-run")
    assert removal.code == 0
    assert removal.out.count("would remove") == 4
    assert box.snapshot() == installed
    assert box.claude_state()["servers"] != {}


# ------------------------------------------------------------------ Claude Code


def test_claude_code_uses_the_plugin_for_the_hosted_server(box: Sandbox) -> None:
    box.stub("claude", CLAUDE_STUB)
    box.stub("curl", CURL_STUB)
    result = box.run("--agents", "claude", key=GOOD_KEY)
    assert result.code == 0, result.text
    assert "installed the plugin relaydeck@relaydeck with the inbox hooks." in result.out
    assert "Saved the API key in secure storage." in result.out
    assert "Inbox hooks are on for: Claude Code." in result.out
    assert "trust the Relaydeck hooks" not in result.out
    assert box.commands("claude") == [
        "plugin list --json",
        "plugin marketplace add Aayush9029/mcp-server",
        "plugin install relaydeck@relaydeck --scope user",
        "plugin configure relaydeck@relaydeck --values-stdin",
    ]
    assert box.claude_state() == {
        "marketplaces": ["Aayush9029/mcp-server"],
        "plugins": ["relaydeck@relaydeck"],
        "key": GOOD_KEY,
        "servers": {},
    }
    for tool in ("claude", "curl"):
        assert all(GOOD_KEY not in " ".join(call["argv"]) for call in box.calls(tool))
    assert not any(call["key_in_env"] for call in box.calls("claude"))
    (curl,) = box.calls("curl")
    assert curl["argv"][-1] == "https://relaydeck.onrender.com/v1/tasks?limit=1"
    assert curl["stdin"] == f'header = "Authorization: Bearer {GOOD_KEY}"\n'

    again = box.run("--agents", "claude", key=SECOND_KEY)
    assert again.code == 0, again.text
    assert "is already installed. Saved the API key again." in again.out
    assert box.commands("claude")[4:] == [
        "plugin list --json",
        "plugin marketplace update relaydeck",
        "plugin update relaydeck@relaydeck --scope user",
        "plugin configure relaydeck@relaydeck --values-stdin",
    ]
    assert box.claude_state()["key"] == SECOND_KEY

    planned = box.run("--agents", "claude", "--uninstall", "--dry-run")
    assert "would remove the plugin relaydeck@relaydeck and the marketplace relaydeck" in planned.out
    removed = box.run("--agents", "claude", "--uninstall")
    assert removed.code == 0, removed.text
    assert "removed the plugin relaydeck@relaydeck and the marketplace relaydeck" in removed.out
    assert box.claude_state() == {"marketplaces": [], "plugins": [], "key": None, "servers": {}}
    assert "Nothing to remove." in box.run("--agents", "claude", "--uninstall").out


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("marketplace", "could not add the plugin marketplace Aayush9029/mcp-server."),
        ("configure", "could not save the API key. In Claude Code, run: /plugin configure relaydeck@relaydeck"),
    ],
)
def test_a_claude_code_failure_is_reported(box: Sandbox, failure: str, message: str) -> None:
    box.stub("claude", CLAUDE_STUB)
    box.stub("curl", CURL_STUB)
    result = box.run("--agents", "claude", key=GOOD_KEY, env={"STUB_FAIL": failure})
    assert result.code == 1
    assert message in result.out
    assert "Failed to" in result.out
    assert "Not complete for 1 agent." in result.out
    assert GOOD_KEY not in result.text


def test_claude_code_uses_mcp_add_for_a_different_server(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("claude", CLAUDE_STUB)
    args = ["--url", key_server.url, "--name", "mine", "--agents", "claude"]
    result = box.run(*args, key=GOOD_KEY)
    assert result.code == 0, result.text
    assert box.calls("claude")[-1]["argv"] == [
        "mcp",
        "add",
        "--transport",
        "http",
        "--scope",
        "user",
        "mine",
        key_server.url,
        "--header",
        f"Authorization: Bearer {GOOD_KEY}",
    ]
    assert not any(command.startswith("plugin") for command in box.commands("claude"))

    rotated = box.run(*args, key=SECOND_KEY)
    assert rotated.code == 0, rotated.text
    assert box.commands("claude")[-2] == "mcp remove mine --scope user"
    assert box.claude_state()["servers"]["mine"]["header"] == f"Authorization: Bearer {SECOND_KEY}"

    removed = box.run(*args, "--uninstall")
    assert removed.code == 0, removed.text
    assert 'removed the server "mine"' in removed.out
    assert box.claude_state()["servers"] == {}


def test_an_agent_without_its_command_fails_with_a_reason(box: Sandbox, key_server: KeyServer) -> None:
    result = box.run("--url", key_server.url, "--agents", "claude,codex,cursor", key=GOOD_KEY)
    assert result.code == 1
    assert "the claude command is not on your PATH. Not changed." in result.out
    assert "the codex command is not on your PATH. Not changed." in result.out
    assert "Not complete for 2 agents." in result.out
    assert (box.home / ".cursor/mcp.json").exists()


# ------------------------------------------------------------------ Codex


def test_codex_gets_a_stored_header(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("codex", CODEX_STUB)
    config = box.codex_home / "config.toml"
    original = 'model = "gpt"\n\n[mcp_servers.docs]\nurl = "https://docs.example/mcp"  # no newline at the end'
    config.write_text(original)
    config.chmod(0o640)
    result = box.run("--url", key_server.url, "--agents", "codex", key=GOOD_KEY)
    assert result.code == 0, result.text
    table = f'[mcp_servers.tasks]\nurl = "{key_server.url}"\nhttp_headers = {{ Authorization = "Bearer {GOOD_KEY}" }}\n'
    assert config.read_text() == original + "\n\n" + table
    assert config.stat().st_mode & 0o777 == 0o600
    assert [backup.read_text() for backup in backups(config)] == [original]
    assert not any("add" in command for command in box.commands("codex"))
    assert all(GOOD_KEY not in " ".join(call["argv"]) for call in box.calls("codex"))


def test_codex_makes_a_new_config_file(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("codex", CODEX_STUB)
    result = box.run("--url", key_server.url, "--agents", "codex", key=GOOD_KEY)
    assert result.code == 0, result.text
    config = box.codex_home / "config.toml"
    assert tomllib.loads(config.read_text()) == {
        "mcp_servers": {"tasks": {"url": key_server.url, "http_headers": {"Authorization": f"Bearer {GOOD_KEY}"}}}
    }
    assert config.read_text().startswith("[mcp_servers.tasks]\n")
    assert backups(config) == []


def test_codex_replaces_an_entry_that_reads_an_environment_variable(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("codex", CODEX_STUB)
    config = box.codex_home / "config.toml"
    config.write_text(f'[mcp_servers.tasks]\nurl = "{key_server.url}"\nbearer_token_env_var = "TASKS_API_KEY"\n')
    result = box.run("--url", key_server.url, "--agents", "codex", key=GOOD_KEY)
    assert result.code == 0, result.text
    assert 'updated "tasks"' in result.out
    servers = tomllib.loads(config.read_text())["mcp_servers"]
    assert servers == {"tasks": {"url": key_server.url, "http_headers": {"Authorization": f"Bearer {GOOD_KEY}"}}}
    assert len(backups(config)) == 1
    assert "TASKS_API_KEY" in backups(config)[0].read_text()


def test_a_malformed_codex_config_is_not_changed(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("codex", CODEX_STUB)
    config = box.codex_home / "config.toml"
    config.write_text("this is [not toml\n")
    for extra in ([], ["--uninstall"]):
        result = box.run("--url", key_server.url, "--agents", "codex", *extra, key=GOOD_KEY)
        assert result.code == 1
        assert "Codex cannot load" in result.out
        assert "Not changed." in result.out
        assert config.read_text() == "this is [not toml\n"
        assert backups(config) == []


def test_codex_gets_its_earlier_file_again_when_it_rejects_the_entry(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("codex", CODEX_STUB)
    config = box.codex_home / "config.toml"
    config.write_text('model = "gpt"\n')
    result = box.run("--url", key_server.url, "--agents", "codex", key=GOOD_KEY, env={"STUB_FAIL": "reject"})
    assert result.code == 1
    assert "Codex did not accept the new entry." in result.out
    assert config.read_text() == 'model = "gpt"\n'


def test_the_codex_plugin_keeps_its_server(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("codex", CODEX_STUB)
    config = box.codex_home / "config.toml"
    original = '[plugins."relaydeck@relaydeck"]\nenabled = true\n'
    config.write_text(original)
    plugin = {"STUB_FAIL": "plugin", "STUB_PLUGIN_URL": key_server.url}
    args = ["--url", key_server.url, "--agents", "codex"]

    nothing = box.run(*args, "--uninstall", env=plugin)
    assert "Nothing to remove." in nothing.out
    assert box.commands("codex") == ["mcp get tasks --json"]

    installed = box.run(*args, key=GOOD_KEY, env=plugin)
    assert installed.code == 0, installed.text
    assert 'added "tasks" to' in installed.out
    servers = tomllib.loads(config.read_text())["mcp_servers"]
    assert servers == {"tasks": {"url": key_server.url, "http_headers": {"Authorization": f"Bearer {GOOD_KEY}"}}}
    assert "No change." in box.run(*args, key=GOOD_KEY, env=plugin).out

    removed = box.run(*args, "--uninstall", env=plugin)
    assert removed.code == 0, removed.text
    assert 'removed "tasks" from' in removed.out
    assert config.read_text() == original
    assert backups(config) == [backups(config)[0]]
    assert backups(config)[0].read_text() == original


def codex_plugins(box: Sandbox) -> Json:
    return read_json(box.state / "codex-plugins.json")


def test_codex_gets_the_plugin_for_the_inbox_hooks(box: Sandbox) -> None:
    box.stub("codex", CODEX_STUB)
    box.stub("curl", CURL_STUB)
    planned = box.run("--agents", "codex", "--dry-run")
    assert "and install the plugin relaydeck@relaydeck for the inbox hooks" in planned.out
    assert planned.out.count("would ") == 1

    result = box.run("--agents", "codex", key=GOOD_KEY)
    assert result.code == 0, result.text
    assert 'added "tasks" to ~/' not in result.out
    assert "Installed the plugin relaydeck@relaydeck for the inbox hooks." in result.out
    assert "Inbox hooks are on for" not in result.out
    assert "Codex: the inbox hooks are installed." in result.out
    assert "Type /hooks in Codex one time and trust the Relaydeck hooks." in result.out
    assert box.commands("codex")[3:] == [
        "mcp get tasks --json",
        "mcp list --json",
        "mcp get tasks --json",
        "plugin list --json",
        "plugin marketplace add Aayush9029/mcp-server",
        "plugin add relaydeck@relaydeck",
    ]
    config = box.codex_home / "config.toml"
    servers = tomllib.loads(config.read_text())["mcp_servers"]
    url = "https://relaydeck.onrender.com/mcp"
    assert servers == {"tasks": {"url": url, "http_headers": {"Authorization": f"Bearer {GOOD_KEY}"}}}
    assert codex_plugins(box) == {"marketplaces": ["Aayush9029/mcp-server"], "installed": ["relaydeck@relaydeck"]}
    assert all(GOOD_KEY not in " ".join(call["argv"]) for call in box.calls("codex"))

    calls = len(box.commands("codex"))
    again = box.run("--agents", "codex", key=GOOD_KEY)
    assert "No change." in again.out
    assert "Each agent was already set up." in again.out
    assert box.commands("codex")[calls:] == [
        "mcp get tasks --json",
        "plugin list --json",
        "plugin marketplace upgrade relaydeck",
        "plugin add relaydeck@relaydeck",
    ]

    planned = box.run("--agents", "codex", "--uninstall", "--dry-run")
    assert "the plugin relaydeck@relaydeck and the marketplace relaydeck" in planned.out
    assert codex_plugins(box)["installed"] == ["relaydeck@relaydeck"]
    removed = box.run("--agents", "codex", "--uninstall")
    assert removed.code == 0, removed.text
    assert 'removed "tasks" from' in removed.out
    assert ", and the plugin relaydeck@relaydeck and the marketplace relaydeck" in removed.out
    assert codex_plugins(box) == {"marketplaces": [], "installed": []}
    assert "mcp_servers" not in tomllib.loads(config.read_text())
    assert "Nothing to remove." in box.run("--agents", "codex", "--uninstall").out


def test_codex_keeps_its_server_when_the_plugin_does_not_install(box: Sandbox) -> None:
    box.stub("codex", CODEX_STUB)
    box.stub("curl", CURL_STUB)
    result = box.run("--agents", "codex", key=GOOD_KEY, env={"STUB_FAIL": "marketplace"})
    assert result.code == 0, result.text
    assert "The plugin relaydeck@relaydeck for the inbox hooks was not installed." in result.out
    assert "Done. 1 agent can now use the server" in result.out
    assert "Inbox hooks are on for" not in result.out
    assert "trust the Relaydeck hooks" not in result.out
    assert "mcp_servers" in tomllib.loads((box.codex_home / "config.toml").read_text())


def test_a_different_codex_server_with_the_same_name_is_not_replaced(box: Sandbox, key_server: KeyServer) -> None:
    box.stub("codex", CODEX_STUB)
    config = box.codex_home / "config.toml"
    original = '[mcp_servers.tasks]\nurl = "https://other.example/mcp"\n'
    config.write_text(original)
    result = box.run("--url", key_server.url, "--agents", "codex", key=GOOD_KEY)
    assert result.code == 1
    assert 'a different server named "tasks"' in result.out
    assert "Left unchanged." in box.run("--url", key_server.url, "--agents", "codex", "--uninstall").out
    assert config.read_text() == original


# ------------------------------------------------------------------ without python3


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_jq_does_the_merge_when_python_is_absent(box: Sandbox, key_server: KeyServer) -> None:
    box.tools = ["jq"]
    path = box.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text(CURSOR_FILE)
    args = ["--url", key_server.url, "--agents", "cursor,gemini"]
    result = box.run(*args, key=GOOD_KEY)
    assert result.code == 0, result.text
    merged, original = read_json(path), json.loads(CURSOR_FILE)
    assert merged == original | {"mcpServers": original["mcpServers"] | {"tasks": entry(key_server.url)}}
    assert list(merged["mcpServers"]) == ["github", "linear", "tasks"]
    assert backups(path)[0].read_text() == CURSOR_FILE
    gemini = read_json(box.home / ".gemini/settings.json")
    assert gemini == {"mcpServers": {"tasks": entry(key_server.url, url_field="httpUrl")}, "hooks": gemini_hooks()}
    assert read_json(box.home / ".cursor/hooks.json") == {"version": 1, "hooks": cursor_hooks()}

    assert box.run(*args, key=GOOD_KEY).out.count("No change.") == 2
    assert box.run(*args, key=SECOND_KEY).out.count("updated") == 2
    assert box.run(*args, "--name", "relay", key=GOOD_KEY).code == 0
    assert read_json(box.home / ".cursor/hooks.json")["hooks"] == cursor_hooks("relay")
    assert box.run(*args, "--name", "relay", "--uninstall").code == 0
    assert box.run(*args, "--uninstall").code == 0
    assert read_json(path) == json.loads(CURSOR_FILE)
    assert read_json(box.home / ".cursor/hooks.json") == {"version": 1}
    assert read_json(box.home / ".gemini/settings.json") == {"mcpServers": {}}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_jq_does_not_change_a_file_with_comments(box: Sandbox, key_server: KeyServer) -> None:
    box.tools = ["jq"]
    path = box.home / ".config/zed/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(ZED_FILE)
    result = box.run("--url", key_server.url, "--agents", "zed", key=GOOD_KEY)
    assert result.code == 1
    assert "is not valid JSON, or it has comments that jq cannot read. Not changed." in result.out
    assert path.read_text() == ZED_FILE


def test_without_python_and_jq_only_a_new_file_is_possible(box: Sandbox, key_server: KeyServer) -> None:
    box.tools = []
    gemini = box.home / ".gemini/settings.json"
    gemini.parent.mkdir()
    gemini.write_text('{"theme": "dark"}\n')
    result = box.run("--url", key_server.url, "--agents", "cursor,gemini", key=GOOD_KEY)
    assert result.code == 1
    assert read_json(box.home / ".cursor/mcp.json") == {"mcpServers": {"tasks": entry(key_server.url)}}
    assert "python3 or jq is necessary to change ~/.gemini/settings.json safely. Not changed." in result.out
    assert gemini.read_text() == '{"theme": "dark"}\n'
    assert "The inbox hooks need python3 or jq, so they were not changed." in result.out
    assert not (box.home / ".cursor/hooks.json").exists()


# ------------------------------------------------------------------ the script itself


def test_shellcheck_is_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    done = subprocess.run(  # noqa: S603  shellcheck comes from PATH on the developer's machine
        [shellcheck, "-s", "sh", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout


def test_the_script_does_not_use_sudo_or_a_shell_profile() -> None:
    code = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
    for word in ("sudo", ".zshrc", ".bashrc", ".profile"):
        assert not any(word in line for line in code)

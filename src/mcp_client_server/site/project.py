"""Files of the repository that the site reads at startup: the docs, the install script, and the plugin manifests."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_GITHUB = re.compile(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$")


@dataclass(frozen=True, slots=True)
class Plugin:
    """One agent plugin that the repository publishes through a marketplace manifest."""

    marketplace: str
    name: str
    source: str
    server_url: str | None
    config_key: str | None = None
    key_env: str | None = None

    @property
    def selector(self) -> str:
        return f"{self.name}@{self.marketplace}"


@dataclass(frozen=True, slots=True)
class Project:
    root: Path
    repository_url: str | None
    claude_plugin: Plugin | None
    codex_plugin: Plugin | None

    @property
    def docs_dir(self) -> Path:
        return self.root / "docs"

    @property
    def install_script(self) -> Path:
        return self.root / "install.sh"


def find_root() -> Path:
    """The repository root: the source checkout that holds this package, else the working directory."""
    checkout = Path(__file__).resolve().parents[3]
    return checkout if (checkout / "docs").is_dir() else Path.cwd()


def _read(path: Path) -> dict[str, object]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _first(value: object) -> dict[str, object]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return _object(items[0]) if items else {}


def _server(manifest: dict[str, object]) -> dict[str, object]:
    servers = _object(manifest.get("mcpServers"))
    return _object(next(iter(servers.values()), None))


def _source(repository: str | None) -> str | None:
    """The marketplace source that the agent CLIs accept: `owner/repo` for GitHub, else the Git URL."""
    if not repository:
        return None
    match = _GITHUB.match(repository)
    return match.group(1) if match else repository


def _claude_plugin(root: Path) -> Plugin | None:
    marketplace = _read(root / ".claude-plugin" / "marketplace.json")
    entry = _first(marketplace.get("plugins"))
    path = _text(entry.get("source"))
    if not path:
        return None
    manifest = _read(root / path / ".claude-plugin" / "plugin.json")
    name, market, source = _text(entry.get("name")), _text(marketplace.get("name")), _source(_repository(manifest))
    if not (name and market and source):
        return None
    config_key = next(iter(_object(manifest.get("userConfig"))), None)
    return Plugin(market, name, source, _text(_server(manifest).get("url")), config_key=config_key)


def _codex_plugin(root: Path) -> Plugin | None:
    marketplace = _read(root / ".agents" / "plugins" / "marketplace.json")
    entry = _first(marketplace.get("plugins"))
    path = _text(_object(entry.get("source")).get("path"))
    if not path:
        return None
    manifest = _read(root / path / ".codex-plugin" / "plugin.json")
    name, market, source = _text(entry.get("name")), _text(marketplace.get("name")), _source(_repository(manifest))
    if not (name and market and source):
        return None
    server = _server(manifest)
    return Plugin(market, name, source, _text(server.get("url")), key_env=_text(server.get("bearer_token_env_var")))


def _repository(manifest: dict[str, object]) -> str | None:
    value = manifest.get("repository")
    return _text(value) or _text(_object(value).get("url"))


def load_project(root: Path) -> Project:
    claude, codex = _claude_plugin(root), _codex_plugin(root)
    repository = _repository(_read(root / "server.json"))
    if not repository:
        for folder in (".claude-plugin", ".codex-plugin"):
            for manifest in sorted(root.glob(f"plugins/*/{folder}/plugin.json")):
                repository = repository or _repository(_read(manifest))
    return Project(root, repository.removesuffix(".git") if repository else None, claude, codex)

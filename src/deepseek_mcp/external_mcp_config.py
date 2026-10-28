"""Opt-in, user-owned configuration for outbound read-only MCP servers.

This is a **separate** private JSON file, not ``config.json``: the provider
config module is at its 500-line limit and external MCP is an explicit opt-in
boundary with its own allowlists. The path defaults to
``~/.deepseek-mcp/external-mcp.json`` and can be overridden by the server-owned
environment variable ``DEEPSEEK_EXTERNAL_MCP_CONFIG``.

Credentials are never stored here. A server may declare header *environment
variable names*; the host resolves them at call time and passes the values to an
isolated child over a private stdin pipe.

No parent/orchestrator MCP configuration is read or inherited.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import windows_file_io

EXTERNAL_CONFIG_ENV = "DEEPSEEK_EXTERNAL_MCP_CONFIG"
DEFAULT_EXTERNAL_CONFIG_PATH = Path.home() / ".deepseek-mcp" / "external-mcp.json"
MAX_EXTERNAL_CONFIG_BYTES = 512 * 1024

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
MAX_REQUEST_TIMEOUT_SECONDS = 120
DEFAULT_MAX_CALLS_PER_RUN = 16
MAX_MAX_CALLS_PER_RUN = 64
MAX_DESCRIPTION_CHARS = 2_000
MAX_INPUT_SCHEMA_BYTES = 16 * 1024
MAX_SERVERS = 16
MAX_TOOLS_PER_SERVER = 32

# Wire caps shared by the parent boundary and the isolated child.
MAX_CALL_REQUEST_BYTES = 512 * 1024
MAX_CALL_RESPONSE_BYTES = 1024 * 1024
MAX_CALL_TEXT_BYTES = 64 * 1024
MAX_HTTP_BODY_BYTES = 4 * 1024 * 1024

_PUBLISHED_PREFIX = "ext"
_SERVER_MAX_CHARS = 24
_TOOL_MAX_CHARS = 32
_SERVER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
_TOOL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)
_CAPABILITIES = frozenset({"coding", "readonly"})

# Git/VCS name refusal is defense in depth, not an exhaustive Git-alias filter:
# the human-configured read-only allowlist and the server-returned annotations
# are authoritative. Only names carrying a VCS identifier or an explicit
# repository-write operation are refused, so generic words such as "status",
# "fetch", "log", or "show" do not overblock non-Git tools.
_VCS_IDENTIFIERS = frozenset({"git", "github", "gitlab", "bitbucket"})
_REPO_WRITE_NAMES = frozenset(
    {
        "create_or_update_file", "create_pull_request", "merge_pull_request",
        "update_pull_request", "update_pull_request_branch", "delete_file",
        "push_files", "create_branch", "create_repository", "fork_repository",
        "create_issue", "update_issue", "add_issue_comment", "create_commit",
        "create_tag", "create_release", "add_reply_to_pull_request_comment",
    }
)
_SERVER_KEYS = frozenset({"name", "url", "headers", "tools", "capabilities"})
_TOOL_KEYS = frozenset({"name", "description", "input_schema", "read_only"})
_TOP_KEYS = frozenset({"servers", "request_timeout_seconds", "max_calls_per_run"})


class ExternalMcpError(RuntimeError):
    """External MCP configuration or call is invalid; fail closed."""


@dataclass(frozen=True)
class ExternalToolSpec:
    server: str
    remote_name: str
    published_name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ExternalServerSpec:
    name: str
    url: str
    tools: tuple[ExternalToolSpec, ...]
    header_env: tuple[tuple[str, str], ...]  # (header name, environment variable)
    capabilities: frozenset[str]


@dataclass(frozen=True)
class ExternalMcpConfig:
    servers: tuple[ExternalServerSpec, ...]
    request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN

    def allows(self, capability: str) -> tuple[ExternalServerSpec, ...]:
        return tuple(
            server for server in self.servers if capability in server.capabilities
        )


def _validate_private_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ExternalMcpError(
            f"cannot inspect external MCP config directory {path}: {exc}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise ExternalMcpError(
            f"external MCP config directory must be a real directory: {path}"
        )
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ExternalMcpError(
            "external MCP config directory must be owned by the current user "
            f"with mode 0700: {path}"
        )


def _validate_private_file(descriptor: int, path: Path) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise ExternalMcpError(f"external MCP config must be a regular file: {path}")
    if os.name != "posix":
        return
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ExternalMcpError(
            "external MCP config must be owned by the current user and private; "
            f"run: chmod 700 {path.parent} && chmod 600 {path}"
        )


def _read_config_text(path: Path) -> str:
    if os.name == "nt":
        try:
            payload, _info = windows_file_io.read_regular(
                path, max_bytes=MAX_EXTERNAL_CONFIG_BYTES
            )
            return payload.decode("utf-8")
        except FileNotFoundError:
            raise
        except UnicodeDecodeError as exc:
            raise ExternalMcpError(f"external MCP config must be UTF-8: {path}") from exc
        except (OSError, windows_file_io.WindowsPathError) as exc:
            raise ExternalMcpError(
                f"cannot safely read external MCP config {path}: {exc}"
            ) from exc
    _validate_private_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ExternalMcpError(
            f"cannot safely open external MCP config {path}: {exc}"
        ) from exc
    try:
        _validate_private_file(descriptor, path)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_EXTERNAL_CONFIG_BYTES + 1)
        if len(payload) > MAX_EXTERNAL_CONFIG_BYTES:
            raise ExternalMcpError(f"external MCP config is too large: {path}")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExternalMcpError(
                f"external MCP config must be UTF-8: {path}"
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class _DuplicateKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for name, value in pairs:
        if name in result:
            raise _DuplicateKey
        result[name] = value
    return result


def _name_tokens(name: str) -> list[str]:
    folded = name.casefold().replace("/", "_").replace(".", "_").replace("-", "_")
    return [token for token in folded.split("_") if token]


def _is_forbidden_git_name(name: str) -> bool:
    """Refuse a VCS identifier or an explicit repository-write tool name."""
    if name.casefold() in _REPO_WRITE_NAMES:
        return True
    return any(token in _VCS_IDENTIFIERS for token in _name_tokens(name))


def _text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalMcpError(f"{label} must be a non-empty string")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ExternalMcpError(f"{label} must not contain control or edge whitespace")
    if len(value) > maximum:
        raise ExternalMcpError(f"{label} exceeds {maximum} characters")
    return value


def _parse_url(label: str, value: object):
    if not isinstance(value, str) or not value:
        raise ExternalMcpError(f"{label} url must be a non-empty string")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ExternalMcpError(f"{label} url must not contain control characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ExternalMcpError(f"{label} url is invalid: {exc}") from exc
    return value, parsed, hostname


def _validate_url(label: str, value: object) -> str:
    text, parsed, hostname = _parse_url(label, value)
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise ExternalMcpError(f"{label} url must have a host and no credentials")
    if parsed.query or parsed.fragment:
        raise ExternalMcpError(f"{label} url must not contain a query or fragment")
    if parsed.scheme == "https":
        return text
    loopback = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and hostname.lower() in loopback:
        return text
    raise ExternalMcpError(
        f"{label} url must use HTTPS; HTTP is allowed only for loopback"
    )


def _validate_server_name(value: object) -> str:
    name = _text(value, "server name", maximum=_SERVER_MAX_CHARS)
    if not set(name).issubset(_SERVER_CHARS):
        raise ExternalMcpError(
            "server name may contain only lowercase letters, digits, '-', '_'"
        )
    if name[0] in "-_" or name[-1] in "-_":
        raise ExternalMcpError("server name must not start or end with '-' or '_'")
    return name


def _validate_remote_tool_name(value: object) -> str:
    name = _text(value, "tool name", maximum=_TOOL_MAX_CHARS)
    if not set(name).issubset(_TOOL_CHARS):
        raise ExternalMcpError("tool name contains unsupported characters")
    if _is_forbidden_git_name(name):
        raise ExternalMcpError(f"Git/VCS tool names are not allowed: {name}")
    return name


def _validate_headers(value: object, label: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ExternalMcpError(f"{label} headers must be an object")
    pairs: list[tuple[str, str]] = []
    for header, env_name in value.items():
        header_name = _text(header, f"{label} header name", maximum=128)
        if any(char in header_name for char in "\r\n:"):
            raise ExternalMcpError(f"{label} header name is invalid")
        variable = _text(env_name, f"{label} header env name", maximum=128)
        if any(char in variable for char in "=") or " " in variable:
            raise ExternalMcpError(f"{label} header env name is invalid")
        pairs.append((header_name, variable))
    return tuple(pairs)


def _validate_input_schema(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ExternalMcpError(f"{label} input_schema must be an object")
    if value.get("type") != "object":
        raise ExternalMcpError(f"{label} input_schema must have type 'object'")
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > MAX_INPUT_SCHEMA_BYTES:
        raise ExternalMcpError(f"{label} input_schema is too large")
    return value


def _published_name(server: str, tool: str) -> str:
    return f"{_PUBLISHED_PREFIX}__{server}__{tool}".replace(".", "_")


def _parse_tool(raw: object, server: str, index: int) -> ExternalToolSpec:
    label = f"server {server} tools[{index}]"
    if not isinstance(raw, dict) or not set(raw).issubset(_TOOL_KEYS):
        raise ExternalMcpError(f"{label} has unsupported fields")
    if raw.get("read_only") is not True:
        raise ExternalMcpError(f"{label} must declare read_only: true")
    name = _validate_remote_tool_name(raw.get("name"))
    description = raw.get("description", f"{server} remote tool {name}")
    description = _text(description, f"{label} description", maximum=MAX_DESCRIPTION_CHARS)
    schema = _validate_input_schema(raw.get("input_schema"), label)
    return ExternalToolSpec(
        server=server,
        remote_name=name,
        published_name=_published_name(server, name),
        description=description,
        input_schema=schema,
    )


def _validate_capabilities(value: object, label: str) -> frozenset[str]:
    if value is None:
        return _CAPABILITIES
    if not isinstance(value, list) or not value:
        raise ExternalMcpError(f"{label} capabilities must be a non-empty list")
    if not all(isinstance(item, str) and item in _CAPABILITIES for item in value):
        raise ExternalMcpError(f"{label} capabilities must be coding and/or readonly")
    if len(value) != len(set(value)):
        raise ExternalMcpError(f"{label} capabilities must not contain duplicates")
    return frozenset(value)


def _parse_server(raw: object, index: int) -> ExternalServerSpec:
    label = f"servers[{index}]"
    if not isinstance(raw, dict) or not set(raw).issubset(_SERVER_KEYS):
        raise ExternalMcpError(f"{label} has unsupported fields")
    name = _validate_server_name(raw.get("name"))
    url = _validate_url(label, raw.get("url"))
    capabilities = _validate_capabilities(raw.get("capabilities"), label)
    headers = _validate_headers(raw.get("headers"), label)
    tools_raw = raw.get("tools")
    if not isinstance(tools_raw, list) or not tools_raw:
        raise ExternalMcpError(f"{label} tools must be a non-empty list")
    if len(tools_raw) > MAX_TOOLS_PER_SERVER:
        raise ExternalMcpError(f"{label} has too many tools")
    tools = tuple(
        _parse_tool(item, name, position) for position, item in enumerate(tools_raw)
    )
    remote_names = [tool.remote_name for tool in tools]
    if len(remote_names) != len(set(remote_names)):
        raise ExternalMcpError(f"{label} has duplicate tool names")
    return ExternalServerSpec(name, url, tools, headers, capabilities)


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExternalMcpError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ExternalMcpError(f"{label} must be between {minimum} and {maximum}")
    return value


def _parse_document(data: object) -> ExternalMcpConfig:
    if not isinstance(data, dict):
        raise ExternalMcpError("external MCP config must be a JSON object")
    if not set(data).issubset(_TOP_KEYS):
        raise ExternalMcpError("external MCP config has unsupported keys")
    servers_raw = data.get("servers", [])
    if servers_raw is None:
        servers_raw = []
    if not isinstance(servers_raw, list):
        raise ExternalMcpError("servers must be a list")
    if len(servers_raw) > MAX_SERVERS:
        raise ExternalMcpError("too many external MCP servers")
    servers = tuple(
        _parse_server(item, index) for index, item in enumerate(servers_raw)
    )
    names = [server.name for server in servers]
    if len(names) != len(set(names)):
        raise ExternalMcpError("duplicate external MCP server name")
    published = [tool.published_name for server in servers for tool in server.tools]
    if len(published) != len(set(published)):
        raise ExternalMcpError("external MCP tool names collide after namespacing")
    return ExternalMcpConfig(
        servers=servers,
        request_timeout_seconds=_bounded_int(
            data.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS),
            "request_timeout_seconds", 1, MAX_REQUEST_TIMEOUT_SECONDS,
        ),
        max_calls_per_run=_bounded_int(
            data.get("max_calls_per_run", DEFAULT_MAX_CALLS_PER_RUN),
            "max_calls_per_run", 1, MAX_MAX_CALLS_PER_RUN,
        ),
    )


def _config_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    configured = os.getenv(EXTERNAL_CONFIG_ENV)
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_EXTERNAL_CONFIG_PATH


def _require_host_owned_location(target: Path, workspace: Path | None) -> None:
    if workspace is None:
        return
    try:
        target.resolve().relative_to(workspace.resolve())
    except ValueError:
        return
    except (OSError, RuntimeError) as error:
        raise ExternalMcpError("cannot resolve external MCP config location") from error
    raise ExternalMcpError("external MCP permissions config must be outside the worker workspace")


def load_external_mcp_config(
    path: Path | None = None, *, workspace: Path | None = None,
) -> ExternalMcpConfig | None:
    """Load the opt-in external MCP config, or ``None`` when none is configured."""
    target = _config_path(path)
    _require_host_owned_location(target, workspace)
    try:
        text = _read_config_text(target)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text, object_pairs_hook=_strict_object)
    except _DuplicateKey:
        raise ExternalMcpError(f"duplicate key in {target}") from None
    except json.JSONDecodeError as exc:
        raise ExternalMcpError(
            f"invalid JSON in {target} (line {exc.lineno}, col {exc.colno})"
        ) from exc
    config = _parse_document(data)
    return config if config.servers else None


def resolve_header_values(server: ExternalServerSpec) -> dict[str, str]:
    """Resolve declared header env references host-side; fail closed if unset."""
    values: dict[str, str] = {}
    for header, variable in server.header_env:
        value = os.getenv(variable)
        if not value:
            raise ExternalMcpError(
                f"external MCP header environment variable {variable} is not set"
            )
        if "\r" in value or "\n" in value:
            raise ExternalMcpError(
                f"external MCP header environment variable {variable} is invalid"
            )
        values[header] = value
    return values

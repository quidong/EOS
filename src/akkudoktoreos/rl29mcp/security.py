"""Settings and guards of the EOS MCP server (RL29 fork).

Everything here is pure and tested on its own:

- ``McpSettings.from_env``: configuration from environment variables; refuses to start
  without a long random token.
- ``BearerAuth``: ASGI middleware, every request needs ``Authorization: Bearer <token>``
  (constant-time comparison); only ``GET /health`` is open (no content).
- ``write_allowed``: configuration writes only below allow-listed paths, never secrets,
  never the adapter/server/general sections.
- ``RateLimiter``: at most ``max_writes_per_minute`` writes.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from akkudoktoreos.server.rest.secrets import path_is_secret

MIN_TOKEN_LENGTH = 32

DEFAULT_WRITE_PATHS = (
    "ems",
    "optimization",
    "load/provider",
    "load/loadml",
    "pvforecast/ensemble",
    "elecprice/ml",
    "database/history/enabled",
    "database/history/actuals",
    "database/history/accuracy",
    "database/history/min_store_interval_minutes",
    "devices/heat_pumps/*/comfort",
    "devices/heat_pumps/*/optimization",
    "devices/heat_pumps/*/dhw",
    "devices/heat_pumps/*/away",
    "devices/batteries/*/min_soc_percentage",
    "devices/batteries/*/max_soc_percentage",
)
"""Configuration sections the MCP may change (``*`` = any one segment)."""

FORBIDDEN_ROOTS = ("adapter", "server", "general", "logging", "cache", "measurement")
"""Never writable, whatever the allow-list says: connections, credentials, location."""

_PATH = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*$")
_KEY = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")


class SecurityError(ValueError):
    """A request the MCP refuses."""


def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _list(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


@dataclass
class McpSettings:
    token: str
    eos_url: str = "http://eos:8503"
    host: str = "0.0.0.0"  # noqa: S104 - the container port is published on the LAN only
    port: int = 8505
    allow_write: bool = False
    write_paths: tuple[str, ...] = DEFAULT_WRITE_PATHS
    allowed_hosts: list[str] = field(default_factory=list)
    report_dir: str = "/data/rl29"
    max_writes_per_minute: int = 10
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if len(self.token or "") < MIN_TOKEN_LENGTH:
            raise SecurityError(
                f"EOS_MCP_TOKEN must be set to a random value of at least {MIN_TOKEN_LENGTH} "
                "characters (e.g. `openssl rand -hex 32`)."
            )
        if not self.eos_url.startswith(("http://", "https://")):
            raise SecurityError("EOS_MCP_EOS_URL must be an http(s) URL.")
        for pattern in self.write_paths:
            if not _PATH.match(pattern.replace("*", "x")):
                raise SecurityError(f"Invalid write path pattern '{pattern}'.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "McpSettings":
        env = os.environ if env is None else env
        paths = _list(env.get("EOS_MCP_WRITE_PATHS"))
        return cls(
            token=env.get("EOS_MCP_TOKEN", ""),
            eos_url=env.get("EOS_MCP_EOS_URL", "http://eos:8503").rstrip("/"),
            host=env.get("EOS_MCP_HOST", "0.0.0.0"),  # noqa: S104
            port=int(env.get("EOS_MCP_PORT", "8505")),
            allow_write=_flag(env.get("EOS_MCP_ALLOW_WRITE")),
            write_paths=tuple(paths) if paths else DEFAULT_WRITE_PATHS,
            allowed_hosts=_list(env.get("EOS_MCP_ALLOWED_HOSTS")),
            report_dir=env.get("EOS_MCP_REPORT_DIR", "/data/rl29"),
            max_writes_per_minute=int(env.get("EOS_MCP_MAX_WRITES_PER_MINUTE", "10")),
        )


def normalize_path(path: str) -> str:
    """``a/b/c`` with no leading/trailing slashes; raises on anything unusual."""
    cleaned = str(path or "").strip().strip("/")
    if cleaned == "":
        return ""
    if not _PATH.match(cleaned) or len(cleaned) > 300:
        raise SecurityError(f"Invalid configuration path '{path}'.")
    return cleaned


def write_allowed(path: str, patterns: tuple[str, ...]) -> bool:
    """True when ``path`` lies at or below one of the allow-listed ``patterns``."""
    path = normalize_path(path)
    if not path or path_is_secret(path):
        return False
    parts = path.split("/")
    if parts[0] in FORBIDDEN_ROOTS:
        return False
    for pattern in patterns:
        pattern_parts = pattern.strip("/").split("/")
        if len(parts) < len(pattern_parts):
            continue
        if all(p == "*" or p == s for p, s in zip(pattern_parts, parts)):
            return True
    return False


def check_key(key: str) -> str:
    if not _KEY.match(str(key or "")):
        raise SecurityError(f"Invalid key '{key}'.")
    return key


def check_value_size(value: Any, limit: int = 64 * 1024) -> None:
    if len(json.dumps(value, default=str)) > limit:
        raise SecurityError("Value too large.")


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self.events: deque[float] = deque()

    def check(self) -> None:
        now = self.clock()
        while self.events and now - self.events[0] > 60.0:
            self.events.popleft()
        if len(self.events) >= self.per_minute:
            raise SecurityError("Too many configuration changes; try again in a minute.")
        self.events.append(now)


ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]


class BearerAuth:
    """ASGI middleware: bearer token on every HTTP request except ``GET /health``."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "GET" and scope.get("path") == "/health":
            await _respond(send, 200, {"status": "ok"})
            return
        header = b""
        for name, value in scope.get("headers") or []:
            if name.lower() == b"authorization":
                header = value
                break
        if not hmac.compare_digest(header, self.expected):
            await _respond(send, 401, {"error": "unauthorized"}, {"www-authenticate": "Bearer"})
            return
        await self.app(scope, receive, send)


async def _respond(send: Any, status: int, body: dict, headers: dict | None = None) -> None:
    payload = json.dumps(body).encode()
    raw_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    for name, value in (headers or {}).items():
        raw_headers.append((name.encode(), value.encode()))
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": payload})

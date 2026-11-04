"""EOS MCP server (RL29 fork): guards, tools, HTTP endpoint."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from akkudoktoreos.rl29mcp.security import (
    McpSettings,
    RateLimiter,
    SecurityError,
    normalize_path,
    write_allowed,
)
from akkudoktoreos.rl29mcp.server import EosClient, EosTools, build_app

TOKEN = "t" * 40


class FakeEos:
    """Minimal EOS REST API: config tree with a secret, solution, history."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {
            "ems": {"interval": 60},
            "optimization": {"algorithm": "MILP", "alternate_algorithms": []},
            "adapter": {"homeassistant": {"token": "real-ha-token", "url": "http://ha"}},
            "database": {"history": {"dsn": "postgresql://eos:pw@db/eos", "enabled": True}},
            "general": {"latitude": 52.5},
        }
        self.saved = 0
        self.requests: list[tuple[str, str]] = []
        self.log_missing = False
        self.log_params: dict[str, str] = {}
        self.log_entries: list[dict[str, Any]] = [
            {
                "time": "2026-11-04T22:00:00+01:00",
                "level": {"name": "INFO", "no": 20},
                "message": "Heat pump wbb12: {'extra_cost_amt': 1.2}",
                "name": "akkudoktoreos.optimization.genetic.genetic",
                "function": "optimize",
                "line": 4737,
                "exception": None,
            },
            {
                "text": "raw",
                "record": {
                    "time": {"repr": "2026-11-04 22:01:00+01:00"},
                    "level": {"name": "ERROR"},
                    "message": "HA call failed: Authorization: Bearer abcdef123 " + "y" * 5000,
                    "name": "akkudoktoreos.adapter",
                    "function": "call",
                    "line": 7,
                    "exception": {"type": "HTTPError", "value": "token=secret123"},
                },
            },
        ]

    def _node(self, path: str) -> Any:
        node: Any = self.config
        for part in [p for p in path.split("/") if p]:
            node = node[part]
        return node

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/v1/health":
            return httpx.Response(200, json={"status": "alive"})
        if path == "/v1/config/file" and request.method == "PUT":
            self.saved += 1
            return httpx.Response(200, json={})
        if path.startswith("/v1/config"):
            sub = path[len("/v1/config") :].strip("/")
            if request.method == "GET":
                try:
                    return httpx.Response(200, json=self._node(sub))
                except KeyError:
                    return httpx.Response(404, json={"detail": "missing"})
            parent, _, leaf = sub.rpartition("/")
            self._node(parent)[leaf] = json.loads(request.content)
            return httpx.Response(200, json={})
        if path == "/v1/energy-management/history/summary":
            return httpx.Response(200, json={"enabled": True, "plan": {"algorithm": "MILP"}})
        if path == "/v1/energy-management/optimization/solution":
            return httpx.Response(
                200,
                json={
                    "generated_at": "x",
                    "solution": {
                        "data": {
                            "2099-01-01T00:00:00+00:00": {
                                "date_time": "2099-01-01T00:00:00+00:00",
                                "grid_consumption_energy_wh": 123.456789,
                                "wbb12_kpi_room_mae_1h_k": 0.1,
                            }
                        }
                    },
                },
            )
        if path == "/v1/logging/log":
            self.log_params = dict(request.url.params)
            if self.log_missing:
                return httpx.Response(404, json={"title": "Log file not found"})
            return httpx.Response(200, json=self.log_entries)
        return httpx.Response(404, json={"detail": "not found"})


def make_tools(allow_write: bool = True, **kwargs: Any) -> tuple[EosTools, FakeEos]:
    fake = FakeEos()
    settings = McpSettings(token=TOKEN, allow_write=allow_write, **kwargs)
    client = EosClient("http://eos", transport=httpx.MockTransport(fake.handler))
    return EosTools(settings, client), fake


def test_settings_require_a_long_token():
    with pytest.raises(SecurityError):
        McpSettings(token="short")
    settings = McpSettings.from_env(
        {
            "EOS_MCP_TOKEN": TOKEN,
            "EOS_MCP_ALLOW_WRITE": "true",
            "EOS_MCP_WRITE_PATHS": "ems, optimization/milp",
            "EOS_MCP_ALLOWED_HOSTS": "eos.local:8505",
        }
    )
    assert settings.allow_write and settings.write_paths == ("ems", "optimization/milp")
    assert settings.allowed_hosts == ["eos.local:8505"]
    assert McpSettings.from_env({"EOS_MCP_TOKEN": TOKEN}).allow_write is False


def test_write_allow_list():
    patterns = ("ems", "optimization", "devices/heat_pumps/*/comfort", "adapter")
    assert write_allowed("ems/interval", patterns)
    assert write_allowed("/optimization/milp/scenarios/", patterns)
    assert write_allowed("devices/heat_pumps/wbb12/comfort/setpoint_c", patterns)
    assert not write_allowed("devices/heat_pumps/wbb12/unit/max_heat_w", patterns)
    assert not write_allowed("adapter/homeassistant/url", patterns)  # forbidden root
    assert not write_allowed("general/latitude", patterns)
    assert not write_allowed("database/history/dsn", ("database",))  # secret
    assert not write_allowed("", patterns)
    for bad in ("../etc", "ems/../adapter", "ems interval", "ems?x=1"):
        with pytest.raises(SecurityError):
            normalize_path(bad)


def test_rate_limiter():
    clock = {"t": 0.0}
    limiter = RateLimiter(2, clock=lambda: clock["t"])
    limiter.check()
    limiter.check()
    with pytest.raises(SecurityError):
        limiter.check()
    clock["t"] = 61.0
    limiter.check()


@pytest.mark.asyncio
async def test_read_tools_never_show_secrets():
    tools, _ = make_tools(allow_write=False)
    config = await tools.config_get()
    assert config["adapter"]["homeassistant"]["token"] == "***"
    assert config["database"]["history"]["dsn"] == "postgresql://eos:***@db/eos"
    assert await tools.config_get("adapter/homeassistant/token") == "***"
    assert await tools.config_get("database/history/dsn") == "postgresql://eos:***@db/eos"
    status = await tools.status()
    assert status["algorithm"] == "MILP" and status["write_enabled"] is False
    plan = await tools.plan(hours=24 * 365 * 100)  # clipped to 168 h: the slot is too far
    assert plan["slots"] == []


@pytest.mark.asyncio
async def test_secret_value_path_is_masked():
    tools, _ = make_tools(allow_write=False)
    # A single secret value read through a path is masked as well.
    value = await tools.config_get("adapter/homeassistant")
    assert value["token"] == "***"


@pytest.mark.asyncio
async def test_write_tools_guarded():
    tools, fake = make_tools(allow_write=False)
    with pytest.raises(SecurityError):
        await tools.config_set("ems/interval", 30, "test")
    tools, fake = make_tools(allow_write=True, max_writes_per_minute=3)
    with pytest.raises(SecurityError):
        await tools.config_set("adapter/homeassistant/token", "x", "steal")
    with pytest.raises(SecurityError):
        await tools.config_set("general/latitude", 0, "move")
    with pytest.raises(SecurityError):
        await tools.config_set("ems/interval", 30, "")  # reason required
    result = await tools.config_set("ems/interval", 30, "15 min test")
    assert result == {"path": "ems/interval", "old": 60, "new": 30, "saved": False}
    assert fake.config["ems"]["interval"] == 30
    undone = await tools.config_undo()
    assert undone["restored"] == 60 and fake.config["ems"]["interval"] == 60
    await tools.config_save()
    assert fake.saved == 1
    with pytest.raises(SecurityError):  # 4th write within a minute
        await tools.config_set("ems/interval", 45, "again")
    assert fake.config["adapter"]["homeassistant"]["token"] == "real-ha-token"


@pytest.mark.asyncio
async def test_reports(tmp_path):
    (tmp_path / "price_ml.json").write_text(json.dumps({"used": True}))
    tools, _ = make_tools(report_dir=str(tmp_path))
    reports = await tools.reports()
    assert reports["price_ml.json"] == {"used": True}
    assert reports["load_ml.json"] is None


def _rpc(
    client: TestClient, method: str, params: dict, token: str | None = TOKEN
) -> httpx.Response:
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return client.post("/mcp", headers=headers, content=json.dumps(body))


def _result(response: httpx.Response) -> dict:
    text = response.text
    if "data:" in text:
        text = [line[5:] for line in text.splitlines() if line.startswith("data:")][-1]
    return json.loads(text)


@pytest.mark.parametrize("allow_write", [False, True])
def test_http_endpoint_requires_token_and_lists_tools(allow_write):
    fake = FakeEos()
    settings = McpSettings(token=TOKEN, allow_write=allow_write)
    app = build_app(settings, EosClient("http://eos", transport=httpx.MockTransport(fake.handler)))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        init = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }
        assert _rpc(client, "initialize", init, token=None).status_code == 401
        assert _rpc(client, "initialize", init, token="x" * 40).status_code == 401
        response = _rpc(client, "initialize", init)
        assert response.status_code == 200, response.text
        tools = _result(_rpc(client, "tools/list", {}))["result"]["tools"]
        names = {tool["name"] for tool in tools}
        assert {"eos_status", "eos_plan", "eos_config_get", "eos_kpis", "eos_logs"} <= names
        assert ("eos_config_set" in names) is allow_write
        call = _result(
            _rpc(client, "tools/call", {"name": "eos_config_get", "arguments": {"path": "adapter"}})
        )
        assert "real-ha-token" not in json.dumps(call)


def test_rounding_keeps_small_values():
    from akkudoktoreos.rl29mcp.server import _round

    assert _round(0.000287345) == 0.000287345
    assert _round(123.4567891) == 123.457
    assert _round(5) == 5


def test_default_allow_list_covers_the_heating_zones():
    from akkudoktoreos.rl29mcp.security import DEFAULT_WRITE_PATHS

    assert write_allowed("devices/heat_pumps/wbb12/zones", DEFAULT_WRITE_PATHS)
    assert write_allowed("devices/heat_pumps/wbb12/zones/zones", DEFAULT_WRITE_PATHS)
    assert write_allowed("devices/heat_pumps/wbb12/zone_setback/enabled", DEFAULT_WRITE_PATHS)
    assert not write_allowed("devices/heat_pumps/wbb12/building", DEFAULT_WRITE_PATHS)


@pytest.mark.asyncio
async def test_logs_are_compact_masked_and_filtered():
    tools, fake = make_tools(allow_write=False)
    result = await tools.logs(limit=10, level="info", contains="Heat pump", minutes=60)
    assert fake.log_params["tail"] == "true"
    assert fake.log_params["level"] == "INFO"
    assert fake.log_params["contains"] == "Heat pump"
    assert "from_time" in fake.log_params
    assert result["count"] == 2
    first, second = result["entries"]
    assert first["where"].endswith("optimize:4737")
    assert second["level"] == "ERROR" and second["time"].startswith("2026-11-04 22:01")
    assert "abcdef123" not in second["message"] and "Bearer ***" in second["message"]
    assert "more]" in second["message"]
    assert "secret123" not in second["exception"]


@pytest.mark.asyncio
async def test_logs_limits_and_validation():
    tools, fake = make_tools(allow_write=False)
    await tools.logs(limit=100000)
    assert fake.log_params["limit"] == "500"
    with pytest.raises(SecurityError):
        await tools.logs(level="LOUD")
    with pytest.raises(SecurityError):
        await tools.logs(contains="x" * 201)


@pytest.mark.asyncio
async def test_logs_without_log_file_explain_how_to_enable():
    tools, fake = make_tools(allow_write=False)
    fake.log_missing = True
    result = await tools.logs()
    assert result["count"] == 0
    assert "logging/file_level" in result["error"]


def test_only_the_log_file_level_is_writable_below_logging():
    from akkudoktoreos.rl29mcp.security import DEFAULT_WRITE_PATHS

    assert write_allowed("logging/file_level", DEFAULT_WRITE_PATHS)
    for path in ("logging", "logging/console_level", "logging/api_level", "logging/file_path"):
        assert not write_allowed(path, DEFAULT_WRITE_PATHS)
    # an explicit allow-list entry for the whole section does not open it either
    assert not write_allowed("logging/console_level", ("logging",))


def test_log_masking():
    from akkudoktoreos.rl29mcp.server import mask_log_text

    text = mask_log_text("url postgresql://eos:pw@db/eos api_key=abc password: 'x1' Bearer t0k")
    assert "pw@" not in text and "abc" not in text and "x1" not in text and "t0k" not in text

"""MCP server for EOS (RL29 fork).

A separate process (own container in the EOS stack) that talks to the EOS REST API and
offers MCP tools: status, plan, forecasts, forecast quality and costs, model reports,
configuration read/change/save. See ``security.py`` for the guards and RL29-MCP.md for
setup.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger

from akkudoktoreos.rl29mcp.security import (
    BearerAuth,
    McpSettings,
    RateLimiter,
    SecurityError,
    check_key,
    check_value_size,
    normalize_path,
    write_allowed,
)
from akkudoktoreos.server.rest.secrets import (
    is_masked,
    mask_value,
    path_is_secret,
    redact,
)

REPORTS = ("load_ml.json", "price_ml.json", "pv_ensemble.json")
DEFAULT_PLAN_EXCLUDE = re.compile(r"_kpi_|_ts$|^date_time$")
MAX_HOURS = 168


class EosError(RuntimeError):
    """EOS answered with an error."""


@dataclass
class EosClient:
    base_url: str
    timeout_s: float = 30.0
    transport: Optional[httpx.AsyncBaseTransport] = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout_s, transport=self.transport
        )

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        async with self._client() as client:
            response = await client.get(path, params=params)
        return _json(response)

    async def put(self, path: str, value: Any = None, params: Optional[dict] = None) -> Any:
        async with self._client() as client:
            if value is None:
                response = await client.put(path, params=params)
            else:
                response = await client.put(path, json=value, params=params)
        return _json(response)


def _json(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        detail = response.text[:500]
        raise EosError(f"EOS {response.status_code}: {detail}")
    try:
        return response.json()
    except ValueError:
        return response.text


@dataclass
class EosTools:
    """The tool implementations (plain async methods, registered in ``build_server``)."""

    settings: McpSettings
    eos: EosClient
    limiter: RateLimiter = field(init=False)
    undo: list[tuple[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.limiter = RateLimiter(self.settings.max_writes_per_minute)

    # ---- read -------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        health = await self.eos.get("/v1/health")
        summary: dict[str, Any] = {}
        try:
            summary = await self.eos.get("/v1/energy-management/history/summary", {"days": 7})
        except Exception as exc:  # history is optional
            summary = {"error": str(exc)}
        algorithm = await self.eos.get("/v1/config/optimization/algorithm")
        alternate = await self.eos.get("/v1/config/optimization/alternate_algorithms")
        return {
            "eos": health,
            "algorithm": algorithm,
            "alternate_algorithms": alternate,
            "latest_plan": summary.get("plan"),
            "history_enabled": summary.get("enabled"),
            "history_error": summary.get("last_error") or summary.get("error"),
            "write_enabled": self.settings.allow_write,
        }

    async def config_get(self, path: str = "") -> Any:
        clean = normalize_path(path)
        value = await self.eos.get(f"/v1/config/{clean}" if clean else "/v1/config")
        if clean and path_is_secret(clean) and not isinstance(value, (dict, list)):
            return mask_value(clean.split("/")[-1], value)
        return redact(value)

    async def plan(self, hours: int = 12, columns: Optional[list[str]] = None) -> dict[str, Any]:
        hours = max(1, min(int(hours), MAX_HOURS))
        solution = await self.eos.get("/v1/energy-management/optimization/solution")
        data = (solution.get("solution") or {}).get("data") or {}
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=hours)
        rows = []
        for stamp, row in sorted(data.items()):
            when = datetime.fromisoformat(stamp)
            if when < now - timedelta(hours=1) or when >= end:
                continue
            if columns:
                picked = {k: v for k, v in row.items() if any(c in k for c in columns)}
            else:
                picked = {k: v for k, v in row.items() if not DEFAULT_PLAN_EXCLUDE.search(k)}
            rows.append({"time": stamp, **{k: _round(v) for k, v in picked.items()}})
        return {
            "generated_at": solution.get("generated_at"),
            "total_costs_amt": solution.get("total_costs_amt"),
            "total_revenues_amt": solution.get("total_revenues_amt"),
            "slots": rows,
        }

    async def forecast(self, key: str, hours: int = 24) -> dict[str, Any]:
        check_key(key)
        hours = max(1, min(int(hours), MAX_HOURS))
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        series = await self.eos.get(
            "/v1/prediction/series",
            {
                "key": key,
                "start_datetime": now.isoformat(),
                "end_datetime": (now + timedelta(hours=hours)).isoformat(),
            },
        )
        data = series.get("data", series) if isinstance(series, dict) else series
        if isinstance(data, dict):
            data = {k: _round(v) for k, v in data.items()}
        return {"key": key, "data": data}

    async def kpis(self, days: int = 30) -> Any:
        days = max(1, min(int(days), 365))
        return await self.eos.get("/v1/energy-management/history/summary", {"days": days})

    async def hours(self, hours: int = 24) -> Any:
        hours = max(1, min(int(hours), MAX_HOURS))
        return await self.eos.get("/v1/energy-management/history/hours", {"hours": hours})

    async def reports(self) -> dict[str, Any]:
        base = Path(self.settings.report_dir)
        result: dict[str, Any] = {}
        for name in REPORTS:
            path = base / name
            try:
                result[name] = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                result[name] = None
            except Exception as exc:
                result[name] = {"error": str(exc)}
        return result

    # ---- write ------------------------------------------------------------------

    def _require_write(self, path: str = "") -> str:
        if not self.settings.allow_write:
            raise SecurityError("Configuration changes are disabled (EOS_MCP_ALLOW_WRITE).")
        if path == "":
            return ""
        clean = normalize_path(path)
        if not write_allowed(clean, self.settings.write_paths):
            raise SecurityError(
                f"'{clean}' may not be changed via MCP (allowed: {', '.join(self.settings.write_paths)})."
            )
        return clean

    async def config_set(self, path: str, value: Any, reason: str) -> dict[str, Any]:
        clean = self._require_write(path)
        if not str(reason or "").strip():
            raise SecurityError("A reason is required for every change.")
        if is_masked(value) or path_is_secret(clean):
            raise SecurityError("Secrets cannot be set via MCP.")
        check_value_size(value)
        self.limiter.check()
        old = await self.eos.get(f"/v1/config/{clean}")
        await self.eos.put(f"/v1/config/{clean}", value)
        new = await self.eos.get(f"/v1/config/{clean}")
        self.undo.append((clean, old))
        del self.undo[:-50]
        logger.info(
            "MCP audit: {}",
            json.dumps(
                {
                    "action": "set",
                    "path": clean,
                    "old": redact(old),
                    "new": redact(new),
                    "reason": reason,
                },
                default=str,
            ),
        )
        return {"path": clean, "old": redact(old), "new": redact(new), "saved": False}

    async def config_undo(self) -> dict[str, Any]:
        self._require_write()
        if not self.undo:
            raise SecurityError("Nothing to undo.")
        self.limiter.check()
        path, old = self.undo.pop()
        await self.eos.put(f"/v1/config/{path}", old)
        logger.info(
            "MCP audit: {}",
            json.dumps({"action": "undo", "path": path, "restored": redact(old)}, default=str),
        )
        return {"path": path, "restored": redact(old), "saved": False}

    async def config_save(self) -> dict[str, Any]:
        self._require_write()
        self.limiter.check()
        await self.eos.put("/v1/config/file")
        logger.info("MCP audit: {}", json.dumps({"action": "save"}))
        return {"saved": True}


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    return value


INSTRUCTIONS = """EOS (Akkudoktor-EOS, RL29 fork) plans battery, heat pump and appliances of the house.
Read tools: eos_status, eos_plan, eos_forecast, eos_kpis, eos_hours, eos_reports, eos_config_get.
Write tools (if enabled): eos_config_set (path + value + reason), eos_config_undo, eos_config_save.
Changes act on the running EOS at once but are only persistent after eos_config_save.
Configuration paths use slashes, e.g. optimization/algorithm or ems/interval. Secrets are never shown."""


def build_server(settings: McpSettings, eos: Optional[EosClient] = None) -> tuple[Any, EosTools]:
    """FastMCP server with the EOS tools; write tools only when enabled."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    tools = EosTools(settings, eos or EosClient(settings.eos_url, settings.timeout_s))
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(settings.allowed_hosts),
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=[],
    )
    server = FastMCP(
        name="eos",
        instructions=INSTRUCTIONS,
        host=settings.host,
        port=settings.port,
        stateless_http=True,
        transport_security=security,
    )

    @server.tool()
    async def eos_status() -> dict[str, Any]:
        """EOS health, active algorithm, latest plan (costs, baseline, saving)."""
        return await tools.status()

    @server.tool()
    async def eos_plan(hours: int = 12, columns: Optional[list[str]] = None) -> dict[str, Any]:
        """Current plan per slot for the next `hours` (max 168).

        `columns`: substrings to select columns, e.g. ["battery", "wbb12_heating_mode"].
        """
        return await tools.plan(hours, columns)

    @server.tool()
    async def eos_forecast(key: str, hours: int = 24) -> dict[str, Any]:
        """Prediction series of a key for the next `hours`.

        Keys e.g. elecprice_marketprice_wh, pvforecast_ac_power, loadforecast_power_w.
        """
        return await tools.forecast(key, hours)

    @server.tool()
    async def eos_kpis(days: int = 30) -> Any:
        """Forecast and plan accuracy, realised grid costs per day and per algorithm.

        Also the latest plan against the self-consumption baseline.
        """
        return await tools.kpis(days)

    @server.tool()
    async def eos_hours(hours: int = 24) -> Any:
        """Hourly actual vs. predicted values of every accuracy pair."""
        return await tools.hours(hours)

    @server.tool()
    async def eos_reports() -> dict[str, Any]:
        """Reports of the learning models: base load ML, price ML, PV ensemble."""
        return await tools.reports()

    @server.tool()
    async def eos_config_get(path: str = "") -> Any:
        """Configuration (whole or below `path`, e.g. optimization/milp); secrets masked."""
        return await tools.config_get(path)

    if settings.allow_write:

        @server.tool()
        async def eos_config_set(path: str, value: Any, reason: str) -> dict[str, Any]:
            """Change one configuration value (allow-listed sections only).

            Returns old and new value. Not persistent until eos_config_save.
            """
            return await tools.config_set(path, value, reason)

        @server.tool()
        async def eos_config_undo() -> dict[str, Any]:
            """Undo the last eos_config_set of this server process."""
            return await tools.config_undo()

        @server.tool()
        async def eos_config_save() -> dict[str, Any]:
            """Write the running configuration to the EOS configuration file."""
            return await tools.config_save()

    return server, tools


def build_app(settings: McpSettings, eos: Optional[EosClient] = None) -> Any:
    """ASGI app: streamable HTTP MCP endpoint ``/mcp`` behind the bearer token."""
    server, _ = build_server(settings, eos)
    return BearerAuth(server.streamable_http_app(), settings.token)

"""Run the EOS MCP server: ``python -m akkudoktoreos.rl29mcp``."""

from __future__ import annotations

import sys

from loguru import logger


def main() -> int:
    import uvicorn

    from akkudoktoreos.rl29mcp.security import McpSettings, SecurityError
    from akkudoktoreos.rl29mcp.server import build_app

    try:
        settings = McpSettings.from_env()
    except SecurityError as exc:
        logger.error("EOS MCP not started: {}", exc)
        return 2
    if not settings.allowed_hosts:
        logger.warning("EOS MCP: EOS_MCP_ALLOWED_HOSTS not set; Host header check is off.")
    logger.info(
        "EOS MCP on {}:{} for {} (write {}).",
        settings.host,
        settings.port,
        settings.eos_url,
        "enabled for " + ", ".join(settings.write_paths) if settings.allow_write else "disabled",
    )
    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        proxy_headers=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

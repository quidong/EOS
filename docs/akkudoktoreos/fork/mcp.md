% SPDX-License-Identifier: Apache-2.0
(fork-mcp-page)=

# Security and MCP access

## Secrets in the REST API

The EOS REST API has no authentication. The fork therefore never returns secrets: every configuration response
(`GET /v1/config`, `GET /v1/config/<path>` and the responses of all configuration PUTs) masks tokens, API keys
and passwords as `***` (in a database DSN only the password). A masked value written back (EOSdash export/import,
a client that writes what it read) leaves the stored secret unchanged. Secrets are set through environment
variables, the configuration file or a PUT with the real value.

Anyone who reaches the API port can still change the configuration. Limit ports 8503/8504 to the hosts that need
them (Home Assistant, the MCP container) with a firewall, and do not expose them to the internet.

## MCP server

A separate process offers EOS as an MCP server (streamable HTTP at `/mcp`). It talks only to the EOS REST API and
reads the model reports from the data volume (read-only). Run it as an extra service with the same image:

```yaml
  eos-mcp:
    image: ghcr.io/quidong/eos:rl29-<commit>      # same image as eos
    restart: unless-stopped
    user: "100:101"
    command: ["python", "-m", "akkudoktoreos.rl29mcp"]
    environment:
      EOS_MCP_TOKEN: ${EOS_MCP_TOKEN}            # openssl rand -hex 32
      EOS_MCP_EOS_URL: http://eos:8503
      EOS_MCP_PORT: "8505"
      EOS_MCP_ALLOWED_HOSTS: <host>:8505
      EOS_MCP_ALLOW_WRITE: "false"
      EOS_MCP_REPORT_DIR: /data/rl29
    volumes: ["eos_data:/data:ro"]
    ports: ["8505:8505"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request as u;
             u.urlopen('http://127.0.0.1:8505/health', timeout=3)"]
```

| Tool | Kind | Content |
| --- | --- | --- |
| `eos_status` | read | health, algorithm (incl. A/B), latest plan with costs and baseline |
| `eos_plan` | read | plan per slot for the next hours, columns selectable |
| `eos_forecast` | read | prediction series of a key (price, PV, load, weather) |
| `eos_kpis`, `eos_hours` | read | forecast/plan accuracy, realised costs (needs the history database) |
| `eos_reports` | read | reports of base load ML, price ML, PV ensemble |
| `eos_config_get` | read | configuration (whole or a subtree), secrets masked |
| `eos_config_set` | write | change one value (allow-listed sections only, reason required) |
| `eos_config_undo`, `eos_config_save` | write | undo the last change, persist the configuration |

Guards:

- **Token**: every request needs `Authorization: Bearer <EOS_MCP_TOKEN>` (≥ 32 characters, constant-time
  comparison); without a valid token the server does not start. Only `GET /health` is open (no content).
- **Host header check** (`EOS_MCP_ALLOWED_HOSTS`) against DNS rebinding from browsers.
- **Read-only by default**; writing needs `EOS_MCP_ALLOW_WRITE=true`.
- **Allow-list** `EOS_MCP_WRITE_PATHS` (default: `ems`, `optimization`, `load/provider`, `load/loadml`,
  `pvforecast/ensemble`, `elecprice/ml`, `database/history/{enabled,actuals,accuracy,min_store_interval_minutes}`,
  `devices/heat_pumps/*/{comfort,optimization,dhw,away}`, `devices/batteries/*/{min,max}_soc_percentage`).
  Never: `adapter`, `server`, `general`, `logging`, `cache`, `measurement` and anything that is a secret.
- **Limits**: `EOS_MCP_MAX_WRITES_PER_MINUTE` (10), values ≤ 64 KB, paths `a/b/c` only.
- **Audit**: every change is logged with old and new value and the reason (`MCP audit: …`); changes are
  effective at once and persistent only after `eos_config_save`.

### Clients

Claude Code:

```bash
claude mcp add --transport http eos http://<host>:8505/mcp \
  --header "Authorization: Bearer <token>"
```

Claude Desktop: `scripts/rl29/mcpb/` builds a desktop extension that asks for address and token at install time:

```bash
cd scripts/rl29/mcpb/server && npm ci --omit=dev && cd ..
npx -y @anthropic-ai/mcpb@2.1.2 pack . eos.mcpb
```

Other clients can use `npx mcp-remote http://<host>:8505/mcp --allow-http --header "Authorization: Bearer <token>"`.
Without TLS, keep the MCP port inside the home network or put a TLS reverse proxy in front.

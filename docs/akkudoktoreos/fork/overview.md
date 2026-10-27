% SPDX-License-Identifier: Apache-2.0
(fork-overview-page)=

# eos-plus fork: overview

The eos-plus fork of [Akkudoktor-EOS](https://github.com/Akkudoktor-EOS/EOS) adds a heat pump model,
a mathematical optimizer, learning forecasts, long-term history and an MCP interface. Everything is
optional: without the corresponding configuration the fork behaves like upstream EOS.

| Area | What the fork adds | Page |
| --- | --- | --- |
| Heat pump | Building, hot water tank, COP, heating rod, modes, holiday, cooling, calibration | {doc}`heatpump` |
| Appliances | Request-driven appliances with deadlines, learned load profiles per program | {doc}`appliances` |
| Optimization | `MILP` (HiGHS) with heat pump model, rolling horizon, re-planning, A/B mode | {doc}`optimization` |
| Forecasts | Base load from own meter and LightGBM, PV ensemble, price forecast after day-ahead | {doc}`forecasts` |
| History | Forecasts, plans and actuals in PostgreSQL/TimescaleDB; accuracy and cost endpoints | {doc}`history` |
| Access | Secrets never leave the REST API; MCP server with token and write allow-list | {doc}`mcp` |
| Grid fees | §14a EnWG modules 1, 2 and 1+3 (time-variable grid fee) | {doc}`heatpump` |
| Start | Compose file, minimal configuration, first plan | {doc}`quickstart` |

## Container image and versions

| Tag | Meaning |
| --- | --- |
| `ghcr.io/quidong/eos:plus-stable` | latest release (recommended) |
| `ghcr.io/quidong/eos:plus-YYYY.MM.N` | a fixed release, see `CHANGELOG-PLUS.md` |
| `ghcr.io/quidong/eos:plus-latest` | latest build of the branch `plus/main` |
| `ghcr.io/quidong/eos:plus-<commit>` | the build of one commit |

The image is a drop-in replacement for the upstream image; the same environment variables apply.
Images are built for `linux/amd64`. The quick start ({doc}`quickstart`) has a complete compose file.

## Principles

- **Opt-in.** New providers, algorithms and device types are only active when configured.
- **Generic.** Devices are identified by their `device_id`; measurement keys follow the pattern
  `<device_id>-<name>` and solution keys `<device_id>_<name>`. Nothing refers to a particular
  installation. Values given as "example" in these pages come from one real system and must be adapted.
- **EOS plans, the controller executes.** EOS decides *what* a device should do (a mode per slot).
  How that reaches the device (relay contacts, Modbus, KNX, a manufacturer API) is the job of the
  controller, typically Home Assistant. EOS knows no contacts, registers or group addresses.
- **Safety in the controller.** Fallbacks when the plan is missing, comfort limits, emergency rules and
  manual overrides belong into the controller, so that a failing EOS never leaves a house cold.

## Testing and updating

```bash
uv sync --extra dev
uv run pytest -q tests/plus                             # fork tests
uv run pytest --finalize --check-config-side-effect -q  # full suite
bash scripts/plus/smoke_test_image.sh ghcr.io/quidong/eos:plus-latest
```

Before an update, save the configuration (`GET /v1/config/backup` or a copy of `EOS.config.json`).
Releases and their changes are listed in `CHANGELOG-PLUS.md`.
Fork-only settings (e.g. `devices.heat_pumps`) make the configuration unusable for upstream images,
so a rollback needs the configuration that matches the image. `scripts/plus/eos_snapshot.py` records
and compares the state of a running EOS before and after an update.

```{toctree}
:maxdepth: 1

quickstart
heatpump
appliances
optimization
forecasts
history
mcp
```

# eos-plus changelog

Changes of the eos-plus fork on top of [Akkudoktor-EOS](https://github.com/Akkudoktor-EOS/EOS).
Releases are tagged `plus-YYYY.MM.N`; each tag publishes `ghcr.io/quidong/eos:plus-YYYY.MM.N` and moves
`:plus-stable`. Every push to `plus/main` publishes `:plus-<commit>` and `:plus-latest`.
Documentation: `docs/akkudoktoreos/fork/` (start with `overview.md` and `quickstart.md`).

## Unreleased

## plus-2026.10.0 (2026-10-28)

First release of the fork.

### Optimization

- `MILP` algorithm (HiGHS via SciPy): battery, grid, appliances and heat pumps (linear building and tank
  model, COP curve, comfort band, legionella, §14a limit) in one model, followed by an exact simulation and an
  optional GENETIC polish. Never worse than the best MILP candidate.
- Two-stage scenarios (low/high PV and load), grid import/export limits, curtailment at negative feed-in.
- Rolling horizon: runs aligned to slot starts, the existing plan is dispatched at every slot start,
  re-planning on SoC, room or hot water deviation; A/B operation of two algorithms.
- 15-minute slots for battery and appliances.

### Devices

- Heat pumps with building, emitter and hot water tank models, configurable modes and signals, heating rod,
  floor cooling with PV surplus, holiday mode with planned heat-up, zone setback, window ventilation,
  standby and ready power, presence-dependent internal gains.
- Automatic calibration of building, COP, emitter, tank loss, hot water draw and airing profiles from
  measurements; plan versus actual inside EOS; KPIs for Home Assistant.
- Request-driven home appliances (deadline from Home Assistant) with learned load profiles per program.
- §14a EnWG grid fee modules (none, 1, 2, 1+3).

### Forecasts

- `LoadMeasured` (profile from the own meter) and `LoadML` (LightGBM quantile model of the base load).
- `PVForecastEnsemble`: PVLib combined with another forecast, weighted by recent error and calibrated.
- Price forecast beyond the published day-ahead prices with LightGBM (`elecprice.ml`), only used when it beats
  the provider's own forecast.
- Public holidays of any country and subdivision (`holiday_region`, e.g. `DE-BW`, `AT`, `NL`; package
  `holidays`). The price model uses the country of the bidding zone when no region is set. The former
  `state` settings (German federal state) still work but are deprecated.

### Operation

- History database (PostgreSQL/TimescaleDB): forecasts, plans and actuals with accuracy per lead time,
  realised costs per algorithm, endpoints for dashboards.
- The REST API never returns secrets (tokens, API keys, DSN passwords are masked).
- MCP server (`python -m akkudoktoreos.mcp`) with bearer token, read-only by default, write allow-list, audit
  log and undo; Claude Desktop extension bundle.
- Home Assistant adapter without the Supervisor (URL + token).
- Example compose file, minimal configuration and quick start; CI starts the image with that configuration.

### Renamed (compatible)

- Fork name `RL29` → `eos-plus`: image tags `plus-*` (the old `rl29-*` images stay available), branch
  `plus/main`, module `akkudoktoreos.mcp` (`akkudoktoreos.rl29mcp` still works), data folder
  `<data>/plus` (an existing `<data>/rl29` is moved once and left as a link), scripts in `scripts/plus`.

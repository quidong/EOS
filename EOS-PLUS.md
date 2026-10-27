# eos-plus fork of Akkudoktor-EOS

This is a public fork of [Akkudoktor-EOS/EOS](https://github.com/Akkudoktor-EOS/EOS). It adds a heat pump model,
a MILP optimizer, learning forecasts (base load, PV, prices), a long-term history with dashboard endpoints,
masking of secrets in the REST API and an MCP server. All additions are opt-in; without configuration the fork
behaves like upstream EOS.

Documentation: [`docs/akkudoktoreos/fork/`](docs/akkudoktoreos/fork/overview.md) (also part of the Sphinx docs).

## Branches and image

| Branch | Purpose |
| --- | --- |
| `main` | Mirror of upstream `main`, never committed to directly |
| `plus/main` | Upstream base + fork commits; every push runs the tests and builds the image |

Images: `ghcr.io/quidong/eos:plus-stable` (latest release), `:plus-YYYY.MM.N` (a release), `:plus-latest` and
`:plus-<commit>` (branch builds). Pin a release or digest in production. Changes per release:
[CHANGELOG-PLUS.md](CHANGELOG-PLUS.md). Quick start: [docs/akkudoktoreos/fork/quickstart.md](docs/akkudoktoreos/fork/quickstart.md).

The fork was called `RL29` before; old names keep working for now: images `rl29-*`, module
`akkudoktoreos.rl29mcp`, data folder `<data>/rl29` (moved to `<data>/plus` once, a link remains).

## Fork-specific code

Kept outside upstream files where possible so that rebasing stays simple; code inside upstream modules is marked
"eos-plus fork".

- `devices/thermal/`, `devices/settings/heatpumpsettings.py`, `devices/heatpump*.py` — heat pump model,
  calibration, plan vs actual, KPIs
- `optimization/genetic/heatpumpgenes.py` — heat pump genes and run plan
- `optimization/milp/` — MILP algorithm incl. linear heat pump model (`thermal.py`)
- `devices/applianceprofiles.py` — learned appliance load profiles; request-driven appliances
- `prediction/loadmeasured.py`, `loadml.py`, `pvforecastensemble.py`, `elecpriceml.py`, `calendarfeatures.py`
- `prediction/gridcontrol14a.py` — §14a EnWG modules
- `core/history.py`, `core/historyrecorder.py`, `core/replan.py`, `core/ems.py` (slot-aligned runs, dispatch
  at slot start)
- `server/rest/secrets.py` — no secrets in configuration responses
- `mcp/` — MCP server (`rl29mcp/` is a compatibility alias)
- `core/forkpaths.py` — data folder of the fork
- `scripts/plus/` — quick start (`quickstart/`), release notes, calibration, plan checks, §14a helper, MILP vs GENETIC comparison, image smoke test,
  snapshot/diff of a running EOS, Claude Desktop extension (`mcpb/`)
- `tests/plus/` — fork tests
- `.github/workflows/plus-image.yml` — tests, image build, smoke test, push to GHCR

## Testing

```bash
uv sync --extra dev
uv run pytest -q tests/plus                                   # fork tests
PLUS_TEST_PG_DSN=postgresql://eos@127.0.0.1:5432/eos_test \
  uv run pytest -q tests/plus/test_history.py                 # history with a database
uv run pytest --finalize --check-config-side-effect -q        # full suite (as in CI)
SMOKE_CONFIG=scripts/plus/quickstart/EOS.config.json \
  bash scripts/plus/smoke_test_image.sh ghcr.io/quidong/eos:plus-latest
```

## Update and rollback

1. Push to `plus/main` → workflow `plus-image` publishes `ghcr.io/quidong/eos:plus-<commit>`.
2. Snapshot the running EOS: `python3 scripts/plus/eos_snapshot.py snapshot http://<eos>:8503 -o before.json`.
3. Back up the configuration (`GET /v1/config/backup` or `EOS.config.json`).
4. Change the image in the stack and redeploy.
5. Compare: `python3 scripts/plus/eos_snapshot.py diff before.json after.json`.

Rollback: previous image plus the configuration that matches it. Upstream images reject fork-only keys
(e.g. `devices.heat_pumps`). Watchtower should not update the container (label
`com.centurylinklabs.watchtower.enable=false`).

## Releases

1. Move the entries of `## Unreleased` in `CHANGELOG-PLUS.md` into a new section `## plus-YYYY.MM.N (date)`
   (N counts the releases of the month from 0) and commit.
2. `git tag plus-YYYY.MM.N && git push origin plus-YYYY.MM.N`, or in the GitHub UI: Releases → Draft a new
   release → new tag `plus-YYYY.MM.N` on `plus/main` → Publish (the workflow fills in the notes).
3. The workflow tests, builds `:plus-YYYY.MM.N` and `:plus-stable`, runs the smoke test with the quick start
   configuration and creates the GitHub release with the changelog section as notes.

## Rebasing onto upstream

```bash
git fetch upstream
git checkout plus/main
git rebase <upstream-commit>
uv run pytest -q tests/plus && uv run pytest --finalize --check-config-side-effect -q
git push --force-with-lease origin plus/main
```

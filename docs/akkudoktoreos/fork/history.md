% SPDX-License-Identifier: Apache-2.0
(fork-history-page)=

# History of forecasts, plans and actuals

Every optimization run can be written to PostgreSQL / TimescaleDB, so that forecast and plan quality can
be measured per lead time, the plan can be compared with a plain self-consumption baseline, the realised
costs of different algorithms can be compared, and learning forecasts have more than a few weeks of data.

| Table | Content |
| --- | --- |
| `eos_run` | one row per stored run: algorithm, duration, planned costs/revenue, baseline, `meta` |
| `eos_forecast` | every prediction column as issued, per target slot with lead time |
| `eos_plan` | every solution column (battery SoC, heat pump modes and temperatures, ...) per target slot |
| `eos_actual` | hourly actuals derived from EOS measurements (`database.history.actuals`) |
| `eos_accuracy` | MAE / bias / RMSE per pair and lead band, written once an hour |

With TimescaleDB the time tables are hypertables (7-day chunks), compressed after 30 days and dropped after
`retention_days` (3 years). Plain PostgreSQL works too. A run is stored at most every
`min_store_interval_minutes` (14), the first `forecast_hours` (48) of it. Writing happens in the background;
a slow or unreachable database is logged and never delays a run.

## Database service

```yaml
services:
  eos-history:
    image: timescale/timescaledb:latest-pg17
    restart: unless-stopped
    environment:
      POSTGRES_USER: eos
      POSTGRES_PASSWORD: ${EOS_HISTORY_PASSWORD}
      POSTGRES_DB: eos
    volumes: ["eos_history:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U eos -d eos"]
  eos:
    environment:
      EOS_DATABASE__HISTORY__DSN: postgresql://eos:${EOS_HISTORY_PASSWORD}@eos-history:5432/eos
    depends_on: [eos-history]
volumes:
  eos_history:
```

Publish no database port: only EOS needs it. Keep the password in a stack variable; the REST API never
shows it (see {doc}`mcp`).

## Configuration

Set the fields one by one (`PUT /v1/config/database/history/enabled`, `.../actuals`, `.../accuracy`). A PUT of
the whole `database/history` object replaces the DSN from the environment with `null` until restart.

```json
{
  "database": {"history": {
    "enabled": true,
    "actuals": [
      {"name": "pv_wh", "keys": ["sensor.<pv_energy_total>"]},
      {"name": "base_load_wh", "keys": ["sensor.<base_load_energy_total>"]},
      {"name": "grid_import_wh", "keys": ["sensor.<grid_import_total>"]},
      {"name": "grid_export_wh", "keys": ["sensor.<grid_export_total>"]},
      {"name": "heatpump_wh", "keys": ["hp1-electric-power"],
       "kind": "mean", "scale": 1.0},
      {"name": "room_c", "keys": ["hp1-room-temperature"],
       "kind": "mean", "scale": 1.0}
    ],
    "accuracy": [
      {"name": "pv", "predicted": "pvforecast_ac_energy_wh", "actual": "pv_wh"},
      {"name": "load", "predicted": "loadforecast_energy_wh",
       "actual": "base_load_wh"},
      {"name": "plan_import", "predicted": "grid_consumption_energy_wh",
       "source": "plan", "actual": "grid_import_wh"},
      {"name": "plan_room", "predicted": "hp1_room_temperature_c", "source": "plan",
       "actual": "room_c", "kind": "level"}
    ]
  }}
}
```

- `kind: meter` (default): cumulative kWh meters known to EOS (`measurement.*_emr_keys` or keys written by the
  adapter); hourly difference × `scale` (1000) = Wh. A negative difference (meter replaced) leaves the hour out.
- `kind: mean`: hourly mean of any measurement (power in W over an hour = Wh, temperatures).
- `subtract`: removes other actual series hour by hour (e.g. base load = house − heat pump).

## Endpoints for dashboards

- `GET /v1/energy-management/history/summary?days=30`: `kpi` (MAE/bias per series and lead time,
  `<name>_mae_<lead>h`), `plan` (latest run: algorithm, duration, planned net costs, baseline, saving),
  `days` (realised net grid costs per day with the algorithm that planned it), `ab` (mean per algorithm).
- `GET /v1/energy-management/history/hours?hours=72&forecast_lead_h=6&plan_lead_h=1`: per hour
  `<name>_actual` / `<name>_predicted` for every accuracy pair.
- `GET /v1/energy-management/history?days=7`: status, accuracy, recent runs.

Home Assistant REST sensors for a dashboard:

```yaml
rest:
  - resource: http://<eos>:8503/v1/energy-management/history/summary?days=30
    scan_interval: 900
    sensor:
      - name: EOS History
        unique_id: eos_history
        value_template: "{{ value_json.state }}"
        device_class: timestamp
        json_attributes: [enabled, last_error, kpi, plan, days, ab]
  - resource: http://<eos>:8503/v1/energy-management/history/hours?hours=72&forecast_lead_h=6&plan_lead_h=1
    scan_interval: 900
    sensor:
      - name: EOS History Hours
        unique_id: eos_history_hours
        value_template: "{{ value_json.state }}"
        device_class: timestamp
        json_attributes: [data]
```

Exclude both sensors from the recorder if the database grows (large attributes).

## Baseline

`baseline_net_amt` is the net cost of the same horizon with plain self-consumption (PV to the load, surplus into
the battery, the rest fed in; deficits from the battery, then the grid), with the plan's own consumption. It shows
what the battery/grid strategy earns; load shifting of the heat pump is judged by the realised costs.

## Tests

```bash
PLUS_TEST_PG_DSN=postgresql://eos@127.0.0.1:5432/eos_test uv run pytest -q tests/plus/test_history.py
```

Without `PLUS_TEST_PG_DSN` the database tests are skipped; CI runs them against a TimescaleDB service.

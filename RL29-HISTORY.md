# RL29: long-term history (forecasts, plans, actuals)

Every optimization run is written to PostgreSQL / TimescaleDB, so that

- forecast and plan quality can be measured per lead time (MAE, bias),
- the plan can be compared with a plain self-consumption baseline,
- forecast models (load, PV ensemble) can learn from more than a few weeks.

Code: `src/akkudoktoreos/core/history.py` (store, metrics),
`src/akkudoktoreos/core/historyrecorder.py` (EMS glue), tests `tests/rl29/test_history.py`.

## Tables

| Table | Content |
| --- | --- |
| `eos_run` | one row per stored run: algorithm, duration, planned costs/revenue, baseline net costs, `meta` (fitness, saving vs. baseline) |
| `eos_forecast` | every prediction column as issued (`pvforecast_ac_energy_wh`, `loadforecast_energy_wh`, `elec_price_amt_kwh`, `feed_in_tariff_amt_kwh`, `weather_air_temp_celcius`, ...) per target slot with lead time |
| `eos_plan` | every solution column (battery SoC, heat pump modes and temperatures, appliance energy, ...) per target slot |
| `eos_actual` | hourly actuals derived from EOS measurements (`database.history.actuals`) |
| `eos_accuracy` | MAE / bias / RMSE per pair and lead band, written once an hour |

With TimescaleDB the time tables are hypertables (7-day chunks), compressed after
30 days and dropped after `retention_days` (default 3 years). Plain PostgreSQL works
too; then EOS deletes old rows itself.

A run is stored at most every `min_store_interval_minutes` (default 14), the first
`forecast_hours` (default 48) of each run. Writing happens in a background task; a
slow or unreachable database is logged (`History: ...`) and never delays a run.

## Stack (Portainer, VM "energy")

Add a database service to the `eos` stack:

```yaml
services:
  eos-history:
    image: timescale/timescaledb:latest-pg17
    restart: unless-stopped
    environment:
      POSTGRES_USER: eos
      POSTGRES_PASSWORD: ${EOS_HISTORY_PASSWORD}
      POSTGRES_DB: eos
    volumes:
      - eos-history:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U eos -d eos"]
      interval: 30s
      timeout: 5s
      retries: 5
    labels:
      - com.centurylinklabs.watchtower.enable=false

  eos:
    # ... existing service ...
    environment:
      EOS_DATABASE__HISTORY__DSN: postgresql://eos:${EOS_HISTORY_PASSWORD}@eos-history:5432/eos
    depends_on:
      - eos-history

volumes:
  eos-history:
```

`EOS_HISTORY_PASSWORD` goes into the stack's environment variables in Portainer.
No port is published: only EOS (and, if wanted, Grafana in the same stack) reach it.

## EOS configuration

```json
{
  "database": {
    "history": {
      "enabled": true,
      "actuals": [
        {"name": "pv_wh",          "keys": ["sensor.energie_pv_erzeugung_gesamt"]},
        {"name": "load_wh",        "keys": ["sensor.energie_hausverbrauch_gesamt"]},
        {"name": "grid_import_wh", "keys": ["sensor.energie_netzbezug_gesamt"]},
        {"name": "grid_export_wh", "keys": ["sensor.energie_einspeisung_gesamt"]},
        {"name": "heatpump_wh",    "keys": ["wbb12-electric-power"], "kind": "mean", "scale": 1.0},
        {"name": "base_load_wh",   "keys": ["sensor.energie_hausverbrauch_gesamt"], "subtract": ["heatpump_wh"]},
        {"name": "room_c",         "keys": ["wbb12-room-temperature"], "kind": "mean", "scale": 1.0}
      ],
      "accuracy": [
        {"name": "pv",        "predicted": "pvforecast_ac_energy_wh", "actual": "pv_wh"},
        {"name": "load",      "predicted": "loadforecast_energy_wh",  "actual": "load_wh"},
        {"name": "plan_load", "predicted": "load_energy_wh", "source": "plan", "actual": "load_wh"},
        {"name": "plan_import", "predicted": "grid_consumption_energy_wh", "source": "plan", "actual": "grid_import_wh"},
        {"name": "plan_room", "predicted": "wbb12_room_temperature_c", "source": "plan", "actual": "room_c", "kind": "level"}
      ]
    }
  }
}
```

- `keys` of kind `meter` are cumulative kWh meters known to EOS (`measurement.*_emr_keys`
  or keys written by the HA adapter); the hourly difference × `scale` (1000) gives Wh.
  A negative difference (meter replaced) leaves the hour out.
- `kind: mean` averages any measurement per hour (temperatures, SoC factor).
- `subtract` removes other actual series hour by hour (base load = house − heat pump).
  The heat pump energy comes from the KNX power measurement (`wbb12-electric-power` =
  `sensor.strom_warmepumpe`, mean W over an hour = Wh), not from the Weishaupt counter,
  which misses the cooling energy.
- Every meter key must reach EOS as a measurement; keys that are not yet imported
  (e.g. the heat pump meter) need an entry in `adapter.homeassistant.device_measurement_entity_ids`
  or `measurement.*_emr_keys` first.

## Grafana (optional)

Dashboard and provisioning live in `scripts/rl29/grafana/`. Add to the `eos` stack:

```yaml
  eos-grafana:
    image: grafana/grafana-oss:latest
    restart: unless-stopped
    environment:
      EOS_HISTORY_PASSWORD: ${EOS_HISTORY_PASSWORD}
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
    volumes:
      - eos-grafana:/var/lib/grafana
      - /opt/eos/grafana/provisioning:/etc/grafana/provisioning:ro
      - /opt/eos/grafana/dashboards:/var/lib/grafana/dashboards:ro
    ports:
      - "3000:3000"
    depends_on:
      - eos-history
```

(`eos-grafana` also goes under `volumes:`; copy `scripts/rl29/grafana/*` to `/opt/eos/grafana/`
on the VM.) The dashboard "EOS – Prognosen, Pläne, Kosten" shows PV and load forecast vs.
actual, MAE and bias per lead time, planned saving vs. baseline, run time, room temperature
and grid import plan vs. actual, and the realised costs per day and algorithm (A/B).
A reverse-proxy entry (e.g. `grafana.example.org` in NPM) can follow.

## Reading the results

- `GET /v1/energy-management/history?days=7` — status, latest accuracy, recent runs with
  baseline, realised net grid costs per day with the planning algorithm (`days`).
- EOS log, once an hour: `History accuracy pv lead 6 h: n=... MAE=... bias=...`.
- SQL, e.g. planned saving against the self-consumption baseline per day:

```sql
SELECT date_trunc('day', issued_at) AS day,
       avg((meta->>'saving_vs_baseline_amt')::float) AS mean_saving_per_run
FROM eos_run GROUP BY 1 ORDER BY 1 DESC;
```

- Realised grid costs per day (actual import × price as forecast 1 h before):

```sql
SELECT date_trunc('day', a.hour) AS day,
       sum(a.value / 1000 * f.price) AS import_costs
FROM eos_actual a
JOIN LATERAL (
  SELECT avg(value) AS price FROM eos_forecast
  WHERE series = 'elec_price_amt_kwh'
    AND target_time >= a.hour AND target_time < a.hour + interval '1 hour'
    AND issued_at <= a.hour - interval '1 hour'
    AND issued_at > a.hour - interval '2 hours'
) f ON true
WHERE a.series = 'grid_import_wh'
GROUP BY 1 ORDER BY 1 DESC;
```

## Baseline

`baseline_net_amt` is the net cost of the same horizon with plain self-consumption:
PV first to the load, surplus into the battery, the rest fed in; deficits from the
battery, then the grid. The load is the plan's own consumption, so the comparison shows
what the battery / grid strategy earns. Heat pump shifting is judged by the realised
costs above, not by this figure.

## Tests

```bash
# local PostgreSQL, or any reachable TimescaleDB
RL29_TEST_PG_DSN=postgresql://eos@127.0.0.1:5432/eos_test uv run pytest -q tests/rl29/test_history.py
```

Without `RL29_TEST_PG_DSN` the database tests are skipped; the CI job starts a
TimescaleDB service and runs them.

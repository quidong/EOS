% SPDX-License-Identifier: Apache-2.0
(fork-quickstart-page)=

# Quick start

This starts eos-plus with EOSdash and a minimal configuration: one PV plane, a battery, day-ahead prices
from Energy-Charts and the MILP optimizer. No heat pump and no Home Assistant are needed; both can be
added later (see {doc}`heatpump`).

## 1. Start the containers

The files are in `scripts/plus/quickstart/` of the repository: `docker-compose.yaml`, `env.example` and
`EOS.config.json`.

```bash
mkdir eos-plus && cd eos-plus
for f in docker-compose.yaml env.example EOS.config.json; do
  curl -fsSLO https://raw.githubusercontent.com/quidong/EOS/plus/main/scripts/plus/quickstart/$f
done
cp env.example .env
sed -i "s/^EOSDASH_SESSKEY=.*/EOSDASH_SESSKEY=$(openssl rand -hex 16)/" .env
docker compose up -d
curl -fsS http://localhost:8503/v1/health
```

`EOS_PLUS_TAG` in `.env` selects the image: `plus-stable` (latest release), a fixed release such as
`plus-2026.10.0`, or `plus-latest` (latest build).

## 2. Load the example configuration

Edit `EOS.config.json` first: location (`general.latitude`, `general.longitude`), PV plane
(`peakpower` in kWp, `surface_azimuth` 180 = south, `surface_tilt`), yearly consumption, the price zone
(`elecprice.energycharts.bidding_zone`), fees, feed-in tariff, battery and inverter. Then:

```bash
curl -fsS -X PUT http://localhost:8503/v1/config \
  -H 'Content-Type: application/json' --data-binary @EOS.config.json -o /dev/null
curl -fsS -X PUT http://localhost:8503/v1/config/file -o /dev/null   # save
```

EOSdash (<http://localhost:8504>) shows and edits the same configuration. EOS plans every 15 minutes
(`ems.interval`); the first plan appears after the forecasts have been fetched:

```bash
curl -fsS http://localhost:8503/v1/energy-management/plan | head -c 600
```

## 3. Optional services

```bash
# History database: set EOS_HISTORY_PASSWORD in .env, then
docker compose --profile history up -d
curl -fsS -X PUT http://localhost:8503/v1/config/database/history/enabled \
  -H 'Content-Type: application/json' -d 'true'

# MCP server: set EOS_MCP_TOKEN (openssl rand -hex 32) in .env, then
docker compose --profile mcp up -d
```

Details: {doc}`history` and {doc}`mcp`.

## 4. Next steps

- Measurements and control through Home Assistant: the adapter settings of upstream EOS
  (`adapter.homeassistant`), without the Supervisor via `base_url` and `token`.
- Learning forecasts: `load.provider: "LoadML"`, `pvforecast.provider: "PVForecastEnsemble"`,
  `elecprice.ml.enabled: true`, `holiday_region` for your country (see {doc}`forecasts`).
- Heat pump, appliances, §14a: {doc}`heatpump`, {doc}`appliances`.

The REST API has no authentication. Keep ports 8503–8505 inside your home network.

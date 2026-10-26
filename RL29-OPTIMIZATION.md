# RL29: forecasts and planning (Okt 2026)

Umsetzung der Optimierungsliste aus dem Chat vom 26.10.2026. Alles ist abschaltbar und
standardmäßig aus; ohne Konfiguration verhält sich EOS wie vorher.

| Phase | Was | Code | Doku/Tests |
| --- | --- | --- | --- |
| 1 | Plan-vs-Ist und Prognosehistorie in TimescaleDB, Baseline-Vergleich | `core/history.py`, `core/historyrecorder.py` | `RL29-HISTORY.md`, `tests/rl29/test_history.py` |
| 2 | Rollierender Horizont: Lauf zu Slotbeginn, SoC-Fallback aus dem letzten Plan | `core/ems.py`, `core/emsettings.py`, `configrequest.py` | `tests/rl29/test_rolling_horizon.py` |
| 3 | Grundlast-ML (LightGBM, Quantile, Feiertage, Urlaub), PV-Ensemble mit Selbstkalibrierung | `prediction/loadml.py`, `prediction/calendarfeatures.py`, `prediction/pvforecastensemble.py` | `tests/rl29/test_load_ml.py`, `tests/rl29/test_pv_ensemble.py` |
| 4 | MILP-Optimierer, umschaltbar neben GENETIC; Szenarien, Netzgrenzen, A/B-Betrieb | `optimization/milp/`, `optimization/optimization.py` | `tests/rl29/test_milp_*.py`, `scripts/rl29/compare_milp_genetic.py` |
| 5 | Neuplanung bei Abweichung, Grafana-Dashboard | `core/replan.py`, `scripts/rl29/grafana/` | `tests/rl29/test_rolling_horizon.py`, `tests/rl29/test_history.py` |

## Phase 2: rollierender Horizont

EOS plant ohnehin alle `ems.interval` Sekunden neu, mit frischen Messwerten (SoC, Raum-,
Estrich-, Speichertemperatur). Zwei Lücken sind geschlossen:

- **Slot-synchroner Lauf** (`ems.run_on_slot_start: true`): der Lauf startet
  `ems.slot_start_offset_sec` (20 s) nach jedem Slotbeginn statt irgendwann im Slot.
  Der erste Slot jedes Plans ist damit (fast) vollständig statt teilweise vergangen.
  `ems.interval` ist dann nur noch das Prüfintervall (60 s).
- **SoC-Fallback**: fehlt ein frischer SoC-Messwert, setzt EOS nicht mehr 0 % an,
  sondern den SoC, den der letzte Plan (nicht älter als 3 h) für diesen Moment
  vorhergesagt hat. 0 % nur noch ohne brauchbaren Plan.

- **Neuplanung bei Abweichung** (`ems.replan_on_deviation: true`): zwischen den Slots wird
  bei jedem Prüfintervall verglichen; weicht der Batterie-SoC um mehr als
  `replan_soc_deviation_pct` (10 %-Punkte), die Raumtemperatur um mehr als
  `replan_temperature_deviation_k` (0,8 K) oder das Warmwasser um mehr als das Vierfache
  davon vom Plan ab, rechnet EOS sofort neu (höchstens alle `replan_min_interval_sec`,
  300 s). Neue Day-ahead-Preise brauchen keinen Auslöser: sie werden in jedem Lauf geholt,
  also spätestens einen Slot nach Veröffentlichung.

Unsicherheitsreserve: über die Quantile der Prognosen (`load.loadml.plan_quantile`,
`pvforecast.ensemble.scale_quantile`), nicht über eine starre SoC-Reserve.
Batterieverschleiß: `devices.batteries.<id>.levelized_cost_of_storage_amt_kwh`
(gibt es schon, wird im GENETIC- und MILP-Modell je geliefertem kWh berechnet).

Fallback bei EOS-Ausfall liegt weiter in HA (RL29-HEATPUMP.md: Plan älter als 45 min →
SG 2/Normal, Push „EOS-Plan fehlt“; Waschmaschine → Frist).

## Phase 3: Prognosen

### `LoadML` (Grundlast)

Lernt die **Grundlast** = Hauszähler − Verbraucher, die EOS selbst plant
(`subtract_power_keys`). Merkmale: Stunde, Wochentag, Tagtyp (Werktag/Samstag/Sonn- und
Feiertag in BW, 24./31.12.), Brückentag, Jahreszeit, Außentemperatur, Urlaubsmodus, Last
derselben Stunde vor einer Woche, Niveau der Vorwoche. Ein LightGBM-Quantilmodell je
Quantil (10/50/90 %). Wird nur benutzt, wenn es auf den letzten `holdout_days` nicht
schlechter ist als das `LoadMeasured`-Profil; sonst `LoadMeasured` (und darunter das
angepasste Standardprofil). Bericht: `<data>/rl29/load_ml.json`.

```json
{
  "load": {
    "provider": "LoadML",
    "loadml": {
      "subtract_power_keys": ["wbb12-electric-power", "waschmaschine-power"],
      "temperature_key": "wbb12-outdoor-temperature",
      "away_key": "wbb12-away",
      "away_return_key": "wbb12-return-ts",
      "plan_quantile": 0.5,
      "state": "BW"
    }
  }
}
```

Voraussetzungen:
- `devices.heat_pumps.wbb12.optimization.load_forecast_includes_heat_pump` bleibt `false`
  (Grundlast ohne WP).
- Referenz für den WP-Verbrauch ist die KNX-Messung, nicht der Zähler der Weishaupt (der erfasst
  nicht alle Verbräuche der WP, z. B. den Kühlstrom). Die Grundlast ist damit
  KNX-Hausverbrauch − KNX-WP. Stunden mit negativer Grundlast (Messlücken) werden verworfen;
  bleiben zu wenige gesunde Tage, bleibt `LoadMeasured` aktiv. Der Bericht zeigt die Zahl der Tage.
- `wbb12-electric-power` (KNX `sensor.strom_warmepumpe`) muss mit Historie in EOS sein
  (steht schon in `device_measurement_history_keys`). Der Heizstab (AC THOR) ist kein
  EOS-Messwert; solange er läuft, gehört er in die Grundlast. Sobald er in EOS geplant wird,
  seinen Leistungsmesswert ergänzen.

### `PVForecastEnsemble`

PVLib (wie bisher) plus optional die Helios-Prognose aus HA (`pvforecast.homeassistant`).
Gewichte ∝ 1/MAE² der letzten `days` Tage gegen den PV-Zähler, danach Kalibrierfaktor =
Quantil des Tagesverhältnisses Messung/Prognose (begrenzt 0,6–1,3). `scale_quantile < 0.5`
plant vorsichtiger. Bericht: `<data>/rl29/pv_ensemble.json`.

```json
{
  "pvforecast": {
    "provider": "PVForecastEnsemble",
    "ensemble": {"members": ["pvlib", "homeassistant"], "days": 14, "scale_quantile": 0.5},
    "homeassistant": {"entity_id": "sensor.<helios_forecast_entity>", "attribute": "forecast"}
  }
}
```

Die PVLib-Ebenen haben `loss: 0.0`; die Kalibrierung gleicht das aus, ohne die
Ebenenkonfiguration anzufassen.

## Phase 4: MILP

`optimization.algorithm: "MILP"` (Rückweg: `"GENETIC"`). Ablauf siehe Docstring von
`optimization/milp/milpoptimization.py`:

1. Dispatch-MILP (HiGHS über SciPy) für Batterie, Netz, Waschmaschine, Endwert der Batterie.
2. Wärmepumpen-Modi: Kandidaten (Status quo, Vorlauf, Heuristiken) und eine
   Koordinatensuche über die Modi je Stunde, jeder Kandidat mit exakter Thermik plus
   optimalem Dispatch bewertet (`hp_search_seconds`).
3. Die besten Kandidaten führen die Startpopulation an; `polish_generations` GENETIC-
   Generationen auf der exakten Simulation. Ergebnis, Spalten, Plan, PDF: wie GENETIC.

```json
{"optimization": {"algorithm": "MILP", "milp": {"hp_search_seconds": 20, "polish_generations": 30, "polish_individuals": 60}}}
```

Erweiterungen (alle optional):

- **Szenarien** (`milp.scenarios: true`): Erwartungswert über drei Szenarien - zentral (50 %),
  wenig PV + hohe Last (25 %), viel PV + niedrige Last (25 %). PV ± `pv_scenario_spread`,
  Last ± `load_scenario_spread` (Standard: die Quantilbreite aus `LoadML`, sonst 20 %).
  Waschmaschinenstart und die ersten `shared_slots` Batterieentscheidungen gelten für alle
  Szenarien, danach passt sich jedes Szenario an (zweistufig). Ergebnis: mehr Reserve dort,
  wo die Prognose unsicher ist.
- **Netzgrenzen**: `milp.max_grid_import_w` (z. B. Hausanschluss) und `max_grid_export_w`
  (PV darüber wird abgeregelt). Ist die Importgrenze mit der festen Last nicht einzuhalten,
  plant der MILP ohne sie und meldet das im Log. Negative Einspeisevergütung führt
  automatisch zum Abregeln statt Einspeisen.
- **A/B-Betrieb**: `optimization.alternate_algorithms: ["GENETIC", "MILP"]` wechselt den
  Algorithmus täglich (lokaler Tag). `GET /v1/energy-management/history?days=30` liefert unter
  `days` die realisierten Netzkosten je Tag und Algorithmus, das Grafana-Dashboard ebenso.
  Achtung: Tage sind nicht gleich (Wetter, Verbrauch); erst über mehrere Wochen aussagekräftig.

Kühlen, §14a-Dimmen und der Heizstab stecken im Wärmepumpenmodell und damit in den
Kandidaten, die der MILP bewertet. Der Heizstab (AC THOR, 3 kW) wird erst eingeplant, wenn
`devices.heat_pumps.wbb12.dhw.rod_power_w` auf 3000 steht (RL29-HEATPUMP.md); das ist ein
Konfigurationsschritt, keine Codeänderung.

Vergleich auf einem synthetischen Tag (96 Slots, Batterie, WP, Waschmaschine):

```bash
uv run python scripts/rl29/compare_milp_genetic.py --generations 400 --individuals 300
```

Nicht im MILP: E-Auto (dann reine GENETIC-Suche), Batterieexport bei Direktvermarktung
(nur im Polish-Schritt).

Ergebnis am 26.10.2026 (96 Slots, GENETIC 300×400): MILP −1,319 € gegen GENETIC −1,315 €
bei 24 s statt 128 s Rechenzeit.

## Einführung (Vorschlag)

1. Image deployen, `database.history` + `eos-history`-Container (RL29-HISTORY.md). Eine
   Woche Daten sammeln, Genauigkeit unter `/v1/energy-management/history` ansehen.
2. `ems.run_on_slot_start: true`, `ems.interval: 60`, `ems.replan_on_deviation: true`.
3. `load.provider: LoadML` und `pvforecast.provider: PVForecastEnsemble`; die Berichte in
   `<data>/rl29/` zeigen, ob ML und Kalibrierung greifen.
4. `optimization.alternate_algorithms: ["GENETIC", "MILP"]` und `milp.scenarios: true`
   für einige Wochen, dann im Dashboard die realisierten Kosten je Algorithmus vergleichen
   und `alternate_algorithms` wieder leeren, `algorithm` auf den Sieger. Rückweg jederzeit
   über die Konfiguration.

# RL29: forecasts and planning (Okt 2026)

Umsetzung der Optimierungsliste aus dem Chat vom 26.10.2026. Alles ist abschaltbar und
standardmäßig aus; ohne Konfiguration verhält sich EOS wie vorher.

| Phase | Was | Code | Doku/Tests |
| --- | --- | --- | --- |
| 1 | Plan-vs-Ist und Prognosehistorie in TimescaleDB, Baseline-Vergleich | `core/history.py`, `core/historyrecorder.py` | `RL29-HISTORY.md`, `tests/rl29/test_history.py` |
| 2 | Rollierender Horizont: Lauf zu Slotbeginn, SoC-Fallback aus dem letzten Plan | `core/ems.py`, `core/emsettings.py`, `configrequest.py` | `tests/rl29/test_rolling_horizon.py` |
| 3 | Grundlast-ML (LightGBM, Quantile, Feiertage, Urlaub), PV-Ensemble mit Selbstkalibrierung | `prediction/loadml.py`, `prediction/calendarfeatures.py`, `prediction/pvforecastensemble.py` | `tests/rl29/test_load_ml.py`, `tests/rl29/test_pv_ensemble.py` |
| 4 | MILP-Optimierer, umschaltbar neben GENETIC; Szenarien, Netzgrenzen, A/B-Betrieb | `optimization/milp/`, `optimization/optimization.py` | `tests/rl29/test_milp_*.py`, `scripts/rl29/compare_milp_genetic.py` |
| 5 | Neuplanung bei Abweichung, Kennzahlen-Endpunkte für HA | `core/replan.py`, `server/eos.py` (`/history/summary`, `/history/hours`) | `tests/rl29/test_rolling_horizon.py`, `tests/rl29/test_history.py` |
| 6 | Wärmepumpe im MILP (lineares Gebäude-/Speichermodell) | `optimization/milp/thermal.py`, `milpoptimization.py` | `tests/rl29/test_milp_thermal.py` |
| 7 | 15-Minuten-Raster, Plan-Werte sofort zum Slotbeginn an HA | `core/ems.py` (`dispatch_current`) | `tests/rl29/test_rolling_horizon.py` |
| 8 | Preisprognose nach dem Day-ahead (LightGBM statt ETS) | `prediction/elecpriceml.py`, `elecpriceabc.py`, `elecpriceenergycharts.py` | `tests/rl29/test_price_ml.py` |

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
  Algorithmus täglich (lokaler Tag). `GET /v1/energy-management/history/summary` liefert unter
  `days` die realisierten Netzkosten je Tag und Algorithmus und unter `ab` den Mittelwert je
  Algorithmus; angezeigt im HA-Dashboard „Energie & Kosten“, Tab „EOS Steuerung“.
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

## Phase 6: Wärmepumpe im MILP

Bisher plante der MILP Akku, Netz und Waschmaschine exakt, die Wärmepumpen-Modi aber nur über
Kandidaten (Status quo, Heuristiken) und eine Koordinatensuche. Jetzt plant ein gemeinsames
MILP zuerst die **Wärme** (`optimization/milp/thermal.py`, `milp.thermal_model`, Standard an):

- Gebäude (Estrich + Raum) und Warmwasserspeicher als lineare Zustandsgleichungen, exakt
  diskretisiert wie in der Simulation; Lüften, Solargewinne, Zapfprofil je Slot.
- Strom der Raumheizung über die COP-Kurve (Stützstellen, konvex), Warmwasser mit dem COP je
  Slot, Heizstab 1:1; Verdichter teilt sich zwischen Heizung und Warmwasser; §14a-Grenze.
- Komfortband (weich, mit den konfigurierten Strafen), Warmwasser-Untergrenze, Legionellen-Frist
  (binär), Warmwasser fertig zur Urlaubsrückkehr, Wert der gespeicherten Wärme am Ende.
- Eigener WP-Zähler (§14a Modul 2): eigener Preis, nicht in der Hausbilanz.

Der optimale Wärmeplan wird in **Modi** übersetzt (das, was HA ausführen kann): Stunde für Stunde
wird jeder erlaubte Modus exakt simuliert und der gewählt, der den MILP-Zuständen am nächsten
kommt. Diese Kandidaten laufen dann wie alle anderen durch die exakte Bewertung; die
Koordinatensuche startet vom besten Kandidaten jeder Familie (Modell und Heuristik) und behält
das bessere Ergebnis. Nicht im Modell: Kühlen (dann wie bisher nur Kandidatensuche).

Ergebnisse (synthetischer Wintertag mit Preistal, Akku, WP, 15 min):

| | ohne WP-Modell | mit WP-Modell |
| --- | --- | --- |
| bester Startkandidat (24 h) | 12,46 € | 11,55 € |
| nach Koordinatensuche (24 h) | 11,44 € | 11,44 € |
| 48 h, 2 s Suchzeit | 25,00 € | 24,34 € |
| GENETIC 300 Gen. × 150 danach | – | −0,002 € |

Heißt: bei unserem 24-h-Horizont findet die Suche dasselbe Optimum, mit dem Modell aber in einem
Bruchteil der Auswertungen; bei längeren Horizonten oder knapper Rechenzeit ist das Modell
deutlich besser. Ein langer GENETIC-Lauf danach findet praktisch nichts mehr: der Plan ist am
erreichbaren Optimum. Legionellenläufe mit Heizstab plant das Modell direkt in die günstigen
Slots vor der Frist.

## Phase 7: 15-Minuten-Raster

`optimization.genetic.interval_sec: 900`. Die Preise kommen ohnehin viertelstündlich; Akku und
Waschmaschine werden je Viertelstunde geplant, die WP-Modi weiter je Stunde (4 Slots je Gen).
Mit `ems.run_on_slot_start` plant EOS alle 15 min neu. Neu: Zum Slotbeginn schickt EOS die Werte
des **bestehenden** Plans für den neuen Slot sofort an HA (`dispatch_current`), der neue Lauf
folgt ~20 s + Rechenzeit später; vorher kamen die Werte erst nach dem Lauf.

Rechenzeit: MILP-Tage ~30 s je Lauf, GENETIC-Tage (300 × 400) deutlich länger als stündlich
(Faktor 2–3); bei Bedarf `genetic.generations` senken. `milp.shared_slots` (4) sind bei 15 min
eine Stunde.

## Phase 8: Preisprognose nach dem Day-ahead

Nach dem letzten veröffentlichten Preis (bis Mitternacht, ab ~13 Uhr bis Mitternacht des
Folgetags) bis zum Ende des Prognosehorizonts (72 h) kam bisher eine ETS-Glättung der
Preishistorie. `elecprice.ml.enabled: true` ersetzt sie durch LightGBM
(`prediction/elecpriceml.py`):

- Merkmale: Tageszeit, Wochentag, Tagtyp (Werktag/Samstag/Sonn- und Feiertag), Jahreszeit,
  Vorlauf, Preis zur selben Zeit am letzten bekannten Tag und eine Woche vorher, Niveau und
  Spanne des letzten Tages, Wochenmittel, Wetter am Standort (Einstrahlung, Wind, Temperatur,
  Bewölkung) zur Zielzeit und am letzten bekannten Tag.
- Trainiert wird wie im Betrieb: für jede vergangene Mitternacht werden die folgenden 72 h aus dem
  damals Bekannten vorhergesagt.
- Wird nur genutzt, wenn es auf den letzten `holdout_days` (7) besser ist als ETS; neu
  trainiert alle 6 h. Bericht: `<data>/rl29/price_ml.json`. Beim ersten Mal holt EOS
  `history_days` (180) Tage Preise nach.

Rückblick auf echte Daten (Energy-Charts DE-LU, 15 min, Wetter am Standort, Stand 26.10.2026):

| Training | Prüfzeitraum | MAE LightGBM | MAE ETS |
| --- | --- | --- | --- |
| 60 Tage | 14 Tage | 3,87 ct/kWh | 5,11 ct/kWh |
| 120 Tage, ohne Wetter | 14 Tage | 4,50 ct/kWh | 5,11 ct/kWh |
| 120 Tage | 14 Tage | 4,34 ct/kWh | 5,11 ct/kWh |
| 175 Tage | 14 Tage | 4,04 ct/kWh | 5,11 ct/kWh |
| 175 Tage | 28 Tage | 4,05 ct/kWh | 5,30 ct/kWh |

Also 15–25 % weniger Fehler bei den Stunden, die über Akku-Restladung und Wärmevorrat für morgen
entscheiden.

```json
{"elecprice": {"ml": {"enabled": true}}, "optimization": {"genetic": {"interval_sec": 900}}}
```

## Einführung (Vorschlag)

1. Image deployen, `database.history` + `eos-history`-Container (RL29-HISTORY.md). Eine
   Woche Daten sammeln, Genauigkeit unter `/v1/energy-management/history` ansehen.
2. `ems.run_on_slot_start: true`, `ems.interval: 60`, `ems.replan_on_deviation: true`.
3. `load.provider: LoadML` und `pvforecast.provider: PVForecastEnsemble`; die Berichte in
   `<data>/rl29/` zeigen, ob ML und Kalibrierung greifen.
4. `optimization.alternate_algorithms: ["GENETIC", "MILP"]` und `milp.scenarios: true`
   für einige Wochen, dann im HA-Dashboard die realisierten Kosten je Algorithmus vergleichen
   und `alternate_algorithms` wieder leeren, `algorithm` auf den Sieger. Rückweg jederzeit
   über die Konfiguration.

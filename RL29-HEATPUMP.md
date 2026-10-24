# Wärmepumpe im RL29-Fork: Inbetriebnahme

Stand: Phase 3 (Optimierer) fertig. Die Home-Assistant-Seite unten ist ein **Entwurf**.
Sie wird erst nach Abstimmung angelegt. Nichts davon ist in HA aktiv.

## 1. Was EOS jetzt rechnet

Pro Wärmepumpe kommen zwei Gene pro Stunde in das Genom:

- ein **Heizmodus** aus `optimization.heating_modes`: 0 REDUCED, 1 NORMAL, 2 RAISED, 3 PREHEAT, 4 FORCED;
- eine **Warmwasser-Option** aus `optimization.dhw_modes`: 0 HOLD, 1 HEAT_PUMP, 2 ROD. Für ROD gibt es eine
  Option je Zieltemperatur in `dhw.rod_targets_c` (z. B. `ROD_60`, `ROD_70`).

Batterie, E-Auto, Geräte und Wärmepumpe werden gemeinsam optimiert. Der Strom von Wärmepumpe und Heizstab läuft
im Energiefluss vor dem Wechselrichter mit, so wie jeder andere Verbraucher.

In die Fitness gehen zusätzlich ein:

- Komfortunterschreitung (€/K·h);
- Warmwasser unter dem Minimum;
- ein verpasster Legionellenlauf;
- Moduswechsel (wenig Relaisschalten);
- der Wert der am Horizontende gespeicherten Wärme: Sie ersetzt Wärme, die der Optimierer sonst in den
  günstigsten Stunden erzeugen würde, und wird deshalb mit dem Mittel des günstigsten Viertels der
  Grenzpreise im Horizont bewertet (`optimization.stored_heat_price_quantile`, Standard 0,25; Stunden mit
  genug PV-Überschuss zählen mit der Einspeisevergütung), geteilt durch den COP. Warmwasser über dem
  WP-Sollwert (Heizstab) bekommt einen Verlustabschlag. Vorher galt der Mittelpreis des ganzen Horizonts,
  der an sonnigen Tagen zum Überheizen am Horizontende verleitet hat.

### Generisch konfigurierbar

Nichts ist auf eine Anlage festgelegt; alles steht je Gerät unter `devices.heat_pumps.<id>`:

| Was | Einstellung | Standard |
|---|---|---|
| Erlaubte Heizmodi | `optimization.heating_modes` | `[0, 1, 2]` |
| Erlaubte Warmwasser-Modi | `optimization.dhw_modes` | `[0, 1, 2]` |
| Heizstab vorhanden | `dhw.rod_power_w` (0 = kein Heizstab, ROD entfällt) | 0 |
| Heizstab-Zieltemperaturen | `dhw.rod_targets_c`, Obergrenze `dhw.rod_max_c` | `[60, 70]`, 70 |
| WW-Sollwert der WP | `dhw.setpoint_c` bzw. Messwert `<id>-dhw-setpoint` | 45 |
| WW-Anhebung bei SG 3 / SG 4 | `dhw.raised_setpoint_c` / `dhw.forced_setpoint_c` (None = keine) | None |
| Höchste WW-Temperatur der WP | `unit.max_dhw_c` | 60 |
| FORCED: Überhöhung über Komfortmax. | `unit.forced_overshoot_k` | 1 K |
| Legionellen | `dhw.legionella_c`, `dhw.legionella_interval_days` (None = aus) | 60 °C, 7 Tage |
| Komfortband | `comfort.setpoint_c`, `comfort.max_c`, `comfort.min_c` (Zeitfenster) | 21 / 22 / 19,5–20,5 |
| Strafen | `optimization.*_penalty_*` | s. Konfigurationsdoku |
| Überheizen über Komfortmax. | `optimization.overheat_penalty_amt_per_kh`; Raumwärme über `comfort.max_c` zählt nicht als Speicher | 0,5 €/Kh |
| Aktiver Modus ohne Wirkung | `optimization.active_mode_penalty_amt_per_h` (RAISED/PREHEAT/FORCED, WW ≠ HOLD) | 0,01 €/h |
| Wert gespeicherter Wärme am Horizontende | `optimization.stored_heat_price_quantile` (günstigster Anteil der Grenzpreise; 1,0 = Mittelpreis) | 0,25 |

Bedeutung der Modi:

| Modus | Bedeutung |
|---|---|
| REDUCED | Raum darf bis Komfortminimum auskühlen |
| NORMAL | Heizkurve wie ohne EOS |
| RAISED | Raumziel Sollwert + `raised_offset_k`; WW nur mit `raised_setpoint_c` |
| PREHEAT | Raumziel Komfortmaximum, alle Ventile offen |
| FORCED | volle Leistung bis Komfortmaximum + `forced_overshoot_k`; WW nur mit `forced_setpoint_c` |
| HEAT_PUMP | WW jetzt auf WP-Sollwert laden |
| ROD_xx | Heizstab bis xx °C |

Welche Signale einen Modus ausführen (SG-Ready-Zustand, Ventile, WW-Befehl), ist **Mapping der
Steuerungsschicht** und wird in HA eingestellt (Abschnitt 4).

Legionellenläufe plant EOS mit allem, was die Temperatur erreicht: der niedrigsten passenden Heizstab-Stufe oder,
ohne Heizstab, einem Heizmodus, dessen WW-Sollwert (`raised_setpoint_c`/`forced_setpoint_c`, begrenzt durch
`unit.max_dhw_c`) reicht. Erreicht nichts die Temperatur, plant EOS keinen Lauf und schreibt das ins Log. Fällig
wird ein Lauf nach `legionella_interval_days` ab dem letzten Messwert ≥ `legionella_c` − 0,5 K; ohne Messhistorie
wird kein Lauf verlangt.

### Schnittstelle EOS → Steuerung

EOS entscheidet nur, **was** die Wärmepumpe tun soll. **Wie** das bei der Anlage ankommt (Relaiskontakte,
Modbus, KNX, Hersteller-API), entscheidet die Steuerung – bei uns Home Assistant (Abschnitt 4). EOS kennt
keine Kontakte, Register oder Gruppenadressen.

Signale je Viertelstunde (Lösungsschlüssel, in HA als `sensor.eos_<schlüssel>`):

| Schlüssel | Bedeutung |
|---|---|
| `<id>_heating_mode` | 0 REDUCED, 1 NORMAL, 2 RAISED, 3 PREHEAT, 4 FORCED |
| `<id>_dhw_mode` | 0 HOLD, 1 HEAT_PUMP, 2 ROD |
| `<id>_rod_target_c` | Heizstab-Zieltemperatur, 0 = aus |
| `<id>_energy_wh`, `<id>_rod_energy_wh` | geplanter Strom WP + Heizstab, Heizstab allein |
| `<id>_room_temperature_c`, `<id>_slab_temperature_c`, `<id>_dhw_temperature_c` | erwartete Temperaturen |

Zusätzlich gibt es FRBC-Anweisungen je Aktor: `<id>_space_heating` mit dem Heizmodus und `<id>_dhw` mit
HOLD, HEAT_PUMP oder ROD_<Temperatur>. In HA erscheinen sie als `sensor.eos_<id>_space_heating` und
`sensor.eos_<id>_dhw`.

## 2. EOS-Konfiguration (Vorschlag für unsere Anlage)

```json
{
  "devices": {
    "max_heat_pumps": 1,
    "heat_pumps": {
      "wbb12": {
        "dhw": {"volume_l": 190, "setpoint_c": 45, "rod_power_w": 3000, "rod_targets_c": [60, 70],
                "legionella_interval_days": 7},
        "unit": {"max_dhw_c": 55},
        "comfort": {"setpoint_c": 21.0, "max_c": 22.0},
        "optimization": {
          "heating_modes": [0, 1, 2],
          "dhw_modes": [0, 1, 2],
          "load_forecast_includes_heat_pump": false
        }
      }
    }
  },
  "adapter": {
    "homeassistant": {
      "device_measurement_entity_ids": {
        "wbb12-room-temperature": "sensor.raumtemperatur_mittel",
        "wbb12-dhw-temperature": "sensor.wh_warmwasser_warmwassertemperatur",
        "wbb12-dhw-setpoint": "sensor.wh_warmwasser_warmwassersolltemperatur"
      },
      "solution_entity_ids": [
        "sensor.eos_wbb12_heating_mode",
        "sensor.eos_wbb12_dhw_mode",
        "sensor.eos_wbb12_rod_target_c",
        "sensor.eos_wbb12_room_temperature_c",
        "sensor.eos_wbb12_dhw_temperature_c"
      ],
      "device_instruction_entity_ids": [
        "sensor.eos_wbb12_space_heating",
        "sensor.eos_wbb12_dhw"
      ]
    }
  }
}
```

Hinweise zur Konfiguration:

- **`rod_power_w`** erst auf 3000 setzen, wenn der Heizstab eingebaut ist. Mit 0 bietet EOS keine Heizstab-Modi an und plant keine Legionellenläufe.
- **Lastprognose ohne Wärmepumpe (umgesetzt 21.10.2026):** `load.loadakkudoktor.loadakkudoktor_year_energy_kwh`
  von 10.000 auf 6.800 kWh/a gesenkt. Grundlage: WP elektrisch laut Weishaupt-Statistik 2.387 kWh vom 1.1.
  bis 21.10.; hochgerechnet auf das Jahr etwa 3.200 kWh. Backup: `/data/rl29/EOS.config.before-load-base.json`.
  Deshalb `load_forecast_includes_heat_pump: false`. Die KNX-Zähler passen noch nicht zusammen
  (19.10.: Hausverbrauch 5,1 kWh, `sensor.energie_warmepumpe` 3,0 kWh, Weishaupt-Statistik 12 kWh), daher
  noch keine messwertgestützte Prognose (`LoadAkkudoktorAdjusted`) auf Basis von `sensor.eos_grundlast_ohne_wp_gesamt`.
- **Gebäudeparameter** kommen aus `scripts/rl29/calibrate_heatpump.py` (erste Kalibrierung) und werden danach
  automatisch aus den Messwerten nachgeführt (Abschnitt 2a). Die Defaults sind Schätzwerte.
- Ein **Estrich-Messwert** existiert nicht. EOS schätzt ihn dann aus dem Gleichgewicht.

## 2a. Automatische Kalibrierung (Fork, ab 22.10.2026)

Gleicher Ansatz wie die PV-Kalibrierung, auf Wärme übertragen: Die konfigurierten Parameter bleiben der
Ausgangspunkt (Prior), die Messwerte eines rollierenden Fensters korrigieren sie, und jedes Ergebnis wird auf
ein Band um die Konfiguration begrenzt. EOS plant mit den angepassten Werten; die Konfiguration selbst wird
nie geschrieben.

| Fit | Daten | Methode |
|---|---|---|
| `building.loss_w_per_k`, `solar_aperture_m2`, `internal_gains_w` | Tagesmittel von Raum-, Außentemperatur, Heizwärme (ohne WW), Globalstrahlung | Regression `Q = H·ΔT − A·G − g`, mit Schrumpfung zur Konfiguration (`prior_weight_days`, Standard 7 Tage); Tage mit wenig Wärme trotz erwartetem Bedarf (WP aus, Handbetrieb) und Tage mit driftender Raumtemperatur (> 1 K) fliegen raus |
| `unit.cop_scale` | Stunden mit Heizwärme ≥ `cop_min_heat_w` (3 kW = kleinste Modulation, WP lief durch) | Energiegewichtetes Verhältnis Messung/Kennfeld, Standby (`unit.standby_w`) vorher abgezogen; Schrumpfung mit `cop_prior_hours` (24 h) |
| `building.emitter_w_per_k` | Stunden mit Heizwärme ≥ 3 kW ohne WW-Ladung | Vorlauf = Raum + Q/`slab_room` + Q/`emitter` + Spreizung/2: aus gemessenem Vorlauf und Wärme wird der Übergang Heizwasser→Estrich gefittet, der den Vorlauf und damit den COP bestimmt; Schrumpfung mit `emitter_prior_hours` (24 h) |
| `dhw.loss_w_per_k` | Nächte 0–5 Uhr ohne Ladung und Zapfung (mindestens 3) | Exponentieller Abfall, Median über Nächte |
| `dhw.draw_profile_wh` (+ `draw_profile_weekend_wh`) | Rohwerte der Speichertemperatur (nicht Stundenmittel), ab 14 brauchbaren Tagen | Zapfung je Messintervall = C·Temperaturabfall − Standby-Verlust (+ gemessene Ladewärme, falls `wbb12-dhw-heat-power` existiert), je Ortsstunde gemittelt; ab je 4 Wochenend- und Werktagen getrennte Profile; Schrumpfung mit 7 Tagen zur Konfiguration; Tage mit Lücken > 3 h und Tage unter 20 % des Median-Tages (Urlaub) fliegen raus; Tagesmenge außerhalb 0,3…3× der Konfiguration → Konfiguration bleibt |

Ohne Globalstrahlung in den Daten bleibt die Apertur auf dem konfigurierten Wert (sonst zählte die Sonne doppelt).

Eingeschaltet am 22.10.2026 (Backup der Config davor: Pi `~/eos-backups/`). Erster Lauf: 49 Stundenwerte,
23 Wetterstunden gefüllt, noch nichts gefittet außer dem Speicherverlust; die Historie wächst mit jedem Lauf.

Konfiguration und Messwerte:

```json
{
  "devices": {"heat_pumps": {"wbb12": {
    "calibration": {"enabled": true, "days": 30, "refit_hours": 24}
  }}},
  "adapter": {"homeassistant": {
    "device_measurement_entity_ids": {
      "wbb12-outdoor-temperature": "sensor.wh_system_aussentemperatur",
      "wbb12-heat-power": "sensor.wh_warmepumpe_warmeleistung",
      "wbb12-electric-power": "sensor.strom_warmepumpe",
      "wbb12-flow-temperature": "sensor.wh_warmepumpe_vorlauftemperatur"
    },
    "device_measurement_history_keys": [
      "wbb12-heat-power", "wbb12-electric-power", "wbb12-flow-temperature",
      "wbb12-outdoor-temperature"
    ]
  }}
}
```

- `wbb12-room-temperature` und `wbb12-dhw-temperature` sind schon da. Optional: `wbb12-dhw-heat-power`
  (Wärme in den Speicher, sonst aus dem Temperaturanstieg geschätzt) und `wbb12-ghi` (Globalstrahlung).
- Stunden ohne gemessene Außentemperatur oder Strahlung füllt EOS aus der Wetterprognose (Open-Meteo, letzte
  48 h) und schreibt sie als Messwerte zurück. Die Strahlung braucht also keinen eigenen Sensor; die Historie
  wächst mit jedem Lauf. `weather_from_prediction: false` schaltet das ab.
- Die `device_measurement_history_keys` sind wichtig: ohne sie sieht EOS nur den Wert beim Lauf, mit ihnen den
  ganzen HA-Verlauf, und die Stundenmittel stimmen. Der aktuelle Zustand wird zusätzlich mit der Laufzeit
  gestempelt, damit die Frischeprüfung (`measurement_max_age_seconds`) auch für History-Keys gilt. Für die
  langsam laufenden Temperaturen (Raum, Warmwasser) reichen die 15-min-Werte der Läufe; sie stehen deshalb
  im Betrieb nicht in den History-Keys.
- Der WP-Strom aus der KNX-Messung (`sensor.strom_warmepumpe`) enthält den Standby; `unit.standby_w` (22 W) wird
  im COP-Fit abgezogen. Der Weishaupt-Zähler wäre hier falsch (kein Kühlstrom, andere Erfassung).

Ergebnis: `<EOS-Datenordner>/rl29/heatpump_calibration.json` (Werte, Diagnose, Zusammenfassung) und im Log
`Heat pump wbb12 calibration over 30 days (...): loss 180 -> 172 W/K, aperture 0.0 -> 2.3 m2, gains 0 -> 150 W
(27 days, MAE 310 -> 140 W); COP scale 1.000 -> 0.91 (143 h, measured 3.4 vs rated 3.7); tank loss 1.50 -> 1.72 W/K
(21 nights)`. Steht dort `nothing fitted yet`, fehlen noch Tage; die Gründe stehen dahinter.

`unit.cop_max` (Standard 7) deckelt den COP bei milder Witterung: Die Nennpunkte decken nur W35/W55 ab, die
Carnot-Extrapolation bei 25 °C Vorlauf liefe sonst auf 8 und machte Heizen an milden Tagen im Modell fast gratis.

Zapfprofil: Ohne gemessene Ladewärme ist eine Zapfung während einer laufenden Ladung unsichtbar; das gelernte
Profil ist dann eher eine Untergrenze. Das Modell ist ein voll durchmischter Speicher, genau dazu passt „Abfall ×
Wärmekapazität“ – auch wenn der Fühler die Schichtung nur teilweise sieht. Das Handskript (`calibrate_heatpump.py`)
arbeitet mit Stundenmitteln und lernt das Zapfprofil nicht.

Stellschrauben (alle unter `calibration`): `fit_building`/`fit_cop`/`fit_dhw`/`fit_emitter`/`fit_draw_profile` (einzeln abschaltbar),
`draw_min_days` (14), `draw_quantile` (None = Mittelwert),
`min_days` (5), `prior_weight_days` (7; 0 = nur Messwerte), `outage_threshold` (0,4; 0 = aus),
`loss_min_factor`/`loss_max_factor` (0,3/3,0), `cop_min_heat_w` (3000), `cop_prior_hours` (24),
`cop_min_scale`/`cop_max_scale` (0,5/1,5).

Erste Kalibrierung von Hand (bevor EOS 30 Tage Historie hat), mit Strahlung aus dem Open-Meteo-Archiv:

```bash
python3 scripts/rl29/calibrate_heatpump.py fetch --ha-url http://homeassistant.local:8123 --token "$HA_TOKEN" \
  --days 30 --open-meteo 52.52,13.40 -o hp.csv
python3 scripts/rl29/calibrate_heatpump.py fit hp.csv --device-id wbb12 --prior-loss 180 --prior-cop-scale 1.0
```

Die Ausgabe ist ein Konfigurations-Snippet für `devices.heat_pumps.wbb12`; die Werte werden dann zum neuen Prior
der automatischen Kalibrierung.

## 2b. Plan gegen Ist (Fork, ab 22.10.2026)

Mit eingeschalteter Kalibrierung zeichnet EOS bei jedem Lauf die ersten 24 h des Wärmepumpenplans auf
(Raum-, Estrich-, Speichertemperatur, Strom, Modus je Slot) und vergleicht stündlich die Pläne des letzten
Tages mit den Messwerten, für zwei Vorlaufzeiten:

- **lead 1h**: der Plan, der kurz vor dem Slot erstellt wurde (folgt die Regelung dem Modell?),
- **lead 6h**: der Plan von mindestens 6 h vorher (sagt das Modell voraus, was passiert?).

Kennzahlen: Raum- und Speichertemperatur als Bias und mittlerer absoluter Fehler (geplanter Wert am Slot-Ende
gegen Messmittel ±30 min), Strom als geplante gegen gemessene kWh mit Verhältnis. Im Log als
`Heat pump wbb12 plan vs actual (last 24 h): lead 1h: room bias +0.15 K, MAE 0.21 K (23 h); ... energy planned
7.3 kWh, measured 6.1 kWh (x0.84) | lead 6h: ...`, dauerhaft in `<EOS-Datenordner>/rl29/heatpump_plan_actual.json`.

Lesart: Ein wachsender Raum-Bias bei lead 6h zeigt auf `building.loss_w_per_k`/Gewinne (die Kalibrierung
sollte ihn mit der Zeit abbauen); ein Energieverhältnis dauerhaft ungleich 1 auf `unit.cop_scale` oder die
Lastannahmen; große Abweichung bei lead 1h, aber nicht bei 6h, auf die HA-Umsetzung (Sperrzeiten, Pause,
Mindesthaltezeit).

### Kennzahlen nach Home Assistant (B5)

Für Wärmepumpen mit eingeschalteter Kalibrierung trägt die Lösung konstante Spalten `<id>_kpi_*`, die der
HA-Adapter wie jeden Lösungswert als `sensor.eos_<id>_kpi_*` setzt. Solange Daten fehlen, steht dort `unknown`
(Plan-Ist ab 12 verglichenen Slots, Kalibrierwerte ab dem jeweiligen Fit) – nichts muss später umgeschaltet werden.

| Spalte | Inhalt |
|---|---|
| `kpi_room_bias_1h_k`, `kpi_room_mae_1h_k`, `kpi_room_bias_6h_k`, `kpi_room_mae_6h_k` | Raum Plan − Ist [K] |
| `kpi_dhw_bias_6h_k` | Speicher Plan − Ist [K] |
| `kpi_energy_ratio_6h` | gemessener / geplanter WP-Strom |
| `kpi_loss_w_per_k`, `kpi_solar_aperture_m2`, `kpi_internal_gains_w`, `kpi_building_days` | Gebäude-Fit |
| `kpi_cop_scale`, `kpi_measured_cop` | COP-Fit |
| `kpi_emitter_w_per_k`, `kpi_dhw_loss_w_per_k` | Heizflächen- und Speicherverlust-Fit |
| `kpi_draw_kwh_per_day`, `kpi_draw_days` | gelerntes Zapfprofil |
| `kpi_airing_open_h_per_day` | gelerntes Lüften (offener Anteil × Stunden pro Tag) |
| `kpi_calibrated` | 1 = mindestens ein Fit aktiv |

In `adapter.homeassistant.solution_entity_ids` eintragen, z. B. `sensor.eos_wbb12_kpi_room_bias_6h_k`,
`sensor.eos_wbb12_kpi_energy_ratio_6h`, `sensor.eos_wbb12_kpi_cop_scale`, `sensor.eos_wbb12_kpi_loss_w_per_k`,
`sensor.eos_wbb12_kpi_draw_kwh_per_day`, `sensor.eos_wbb12_kpi_calibrated`. Fehlende Werte schickt der Adapter
als `unknown` statt NaN.

## 2c. PDF-Bericht

`GET /v1/energy-management/optimization/solution/GENETIC/pdf` enthält zusätzlich zu den Upstream-Seiten je
Wärmepumpe: Raum-/Estrichtemperatur mit Komfortband und Außentemperatur, Warmwasser mit Heizstab-Ziel, Strom
(WP + Heizstab) und COP/EER, die Modusfolge als Text; dazu je Wärmepumpe die Plan-Ist-Auswertung und das letzte
Kalibrierungsergebnis sowie je Gerät und Programm das gelernte Lastprofil (aus den JSON-Dateien in `/data/rl29`).

```bash
curl -s http://eos.local:8503/v1/energy-management/optimization/solution/GENETIC/pdf -o eos-plan.pdf
```

## 2d. Urlaubsmodus (Fork + HA, ab 23.10.2026)

Ziel: Während der Abwesenheit spart das Haus Energie; zur Rückkehr ist es warm und Warmwasser ist da, aufgeheizt in
den günstigen bzw. PV-Stunden davor.

**EOS** (`devices.heat_pumps.<id>.away`): Messwerte `<id>-away` (1 = Urlaub), `<id>-return-ts` (Unix-Zeit der
Rückkehr, 0 = unbekannt → ganzer Horizont Urlaub) und `<id>-away-room-temperature` (Raumtemperatur im Urlaub).
Bis zur Rückkehr gilt: Komfortminimum und NORMAL-Sollwert = Urlaubstemperatur, RAISED heizt auf den normalen
Sollwert (Hochheizen auch ohne PREHEAT), kein Zapfprofil; mit `away.dhw_off` zusätzlich kein Nachheizen durch den
WP-Regler und kein Speicher-Minimum. Ab der Rückkehr gilt das normale Band; ein Speicher unter dem Minimum zur
Rückkehr zählt wie zwei Stunden Unterschreitung. Der Optimierer legt Hochheizen und Ladung deshalb vor die Rückkehr,
sobald sie im Horizont liegt (72 h). Kühlsaison: bis zur Rückkehr bis `away.cooling_max_c` (28 °C) zulässig.
Lösungsschlüssel `<id>_away` (1 = Slot vor der Rückkehr). Einstellungen: `room_c` (20, Rückfall), `dhw_off`,
`cooling_max_c`, `max_age_hours` (48).

**HA** (angelegt 23.10.2026):

| Objekt | Zweck |
|---|---|
| `input_boolean.urlaubsmodus` | Urlaubsmodus an/aus (→ `wbb12-away`) |
| `input_datetime.urlaub_ruckkehr` | Rückkehr (Datum + Uhrzeit) |
| `sensor.eos_urlaub_ruckkehr_zeitstempel` | Rückkehr als Unix-Zeit, 0 ohne Urlaub (→ `wbb12-return-ts`) |
| `number.wh_heizkreis2_raumsolltemperatur_absenk2` | Absenktemperatur des Heizkreises = Urlaubstemperatur (→ `wbb12-away-room-temperature`) |
| `sensor.eos_wp_sg_ready_soll` | im Urlaub: EOS Reduziert/Normal → SG 1 (Absenk); Absenksperre/-dauer gelten nicht; Untergrenze kältester Raum = Absenktemperatur − 1 K |
| `automation.eos_warmepumpe_heizmodus_umsetzen` | im Urlaub: Räume auf die Absenktemperatur, EOS Angehoben → Raumsoll Normal; Wechsel des Urlaubsmodus setzt sofort um |
| `automation.urlaubsmodus_start_ruckkehr_hochheizen` | Push Start/Ende, beendet den Urlaubsmodus zur Rückkehrzeit, meldet einmal den Beginn des Hochheizens |
| `input_boolean.urlaub_hochheizen_gemeldet` | Merker „Hochheizen gemeldet“ |

EOS-Konfiguration (eingespielt 23.10.2026, zusammen mit `away.dhw_off: true`):

```json
"adapter": {"homeassistant": {
  "device_measurement_entity_ids": {
    "wbb12-away": "input_boolean.urlaubsmodus",
    "wbb12-return-ts": "sensor.eos_urlaub_ruckkehr_zeitstempel",
    "wbb12-away-room-temperature": "number.wh_heizkreis2_raumsolltemperatur_absenk2",
    "wbb12-away-min-temperature": "input_number.urlaub_standby_untergrenze_raum"
  },
  "solution_entity_ids": ["...", "sensor.eos_wbb12_away"]
}}
```

**WP-Standby, WLAN, Geräte** (`automation.urlaubsmodus_wp_standby_wlan_gerate`, angelegt 23.10.2026):

- Systembetriebsart der WP auf **Standby** (Heizung und Warmwasser im Frostschutz), solange EOS „Reduziert“ und
  keine Warmwasserladung plant, der kälteste Raum über `input_number.urlaub_standby_untergrenze_raum` (16 °C) liegt
  und die Rückkehr weiter als `input_number.urlaub_wiederanlauf_vor_ruckkehr` (4 h) entfernt ist. Plant EOS
  Normal/Angehoben oder eine Ladung, geht die WP zurück in ihre vorherige Betriebsart
  (`input_text.urlaub_wp_betriebsart_vorher`). Ist EOS veraltet, bleibt Standby; die Raum-Untergrenze gilt weiter.
- EOS weiß davon über `away.standby_min_c` bzw. den Messwert `<id>-away-min-temperature`
  (→ `input_number.urlaub_standby_untergrenze_raum`): Bis zur Rückkehr darf REDUCED das Haus bis zu dieser Grenze
  auskühlen, NORMAL hält die Absenktemperatur, RAISED heizt auf den Normal-Sollwert. Mit Standby ist auch das
  Warmwasser aus → `away.dhw_off: true`.
- WLAN: PoE der Access Points aus (`switch.urlaub_wlan_access_points_poe` = OG Port 1, 2; UG Port 3, 9, 12).
- Geräte: PoE der beiden 2N-Innenstationen (OG Port 7, UG Port 2) aus (`switch.urlaub_gerate_poe`); die Türstation
  (UG Port 1) bleibt an.
- Kalender-Pi: `script.urlaub_kalender_herunterfahren` ruft `rest_command.kalender_herunterfahren` (POST
  `http://calendar.local:8099/shutdown`, kleine API auf dem Pi, Quelle `/opt/calendar-api` auf dem Server-Pi,
  nur von HA und dem Server-Pi aus und nur mit Schlüssel: `Authorization: Bearer …` aus
  `/etc/calendar-api/token`, in HA als `!secret calendar_api_auth`) und schaltet 5 min später UG Port 4 ab. Beim Wiederanlauf Port 4 an →
  der Pi fährt hoch. Ohne erreichbare API wird der Port trotzdem nach 5 min abgeschaltet.
- Warmwasser auf 20 °C: erst „WW Absenk“, dann „WW Normal“ – die Integration hält Normal ≥ Absenk und übernimmt die
  neue Grenze erst nach einem Neuladen, deshalb wird `weishaupt_modbus` dazwischen neu geladen. Zurück in
  umgekehrter Reihenfolge (erst Normal, dann Absenk). Werte vorher in `input_number.urlaub_ww_normal_vorher` /
  `urlaub_ww_absenk_vorher`.
- Zum Wiederanlauf (Rückkehr − 4 h) bzw. beim Ende des Urlaubs: WP zurück, PoE an, WW-Absenk zurück, Push „Haus
  fährt hoch“. WLAN-Geräte (Shellys, Waschmaschine, Saugroboter, ESP32-BT-Proxy, Tonieboxen) sind bis dahin offline;
  die Shellys behalten ihren Schaltzustand.

## 2e. OG-Absenkung und Lüften (Fork + HA, ab 23.10.2026)

**OG-Absenkung (`devices.heat_pumps.<id>.zone_setback`):** Alle Räume im Obergeschoss laufen nachts (22–24 und
0–12 Uhr werktags, 0–8 Uhr am Wochenende) auf der Absenktemperatur des Heizkreises
(`number.wh_heizkreis2_raumsolltemperatur_absenk2`, über den Messwert `<id>-setback-room-temperature`; wer sie an
der WP ändert, ändert auch die OG-Absenkung). EOS rechnet mit einer Zone: Raumtemperatur = Mittel aller Räume. In
den Absenkzeiten sinken deshalb NORMAL-Sollwert, RAISED-Ziel und Komfortmaximum um `share` (6 von 15 Räumen = 0,4)
mal den Abstand zur Absenktemperatur, das Komfortminimum entsprechend bis Absenktemperatur − `min_offset_k`
(0,5 K). Ohne das würde EOS die kühleren OG-Räume als Komfortverletzung lesen und gegen die Absenkung anheizen.
Lösungsschlüssel `<id>_zone_setback` (1 = Absenkung) sagt HA, wann die OG-Regler auf die Absenktemperatur gehen.
Nicht im Urlaub (dann sind alle Räume abgesenkt) und nicht in der Kühlsaison. Die Zeiten stehen in
`zone_setback.windows` (TimeWindows mit `day_of_week`).

**Lüften (`devices.heat_pumps.<id>.ventilation`):** Messwert `<id>-window-open` = Anteil offener Fenster (0..1, aus
den Fensterkontakten). Modell: Lüften ist ein zusätzlicher Verlust des Raumknotens, `loss_w_per_k` (1000 W/K bei
allen Fenstern weit offen) × Anteil × (Raum − Außen). Erwartetes Lüften je Stunde = `profile` (offener Anteil ×
Stunden, Standard 0,1 um 7 und 21 Uhr); offene Fenster jetzt zählen im ersten Slot.

- Kalibrierung: lernt das Profil aus `<id>-window-open` ab 14 Tagen (Werktag/Wochenende getrennt ab je 4 Tagen,
  Schrumpfung 7 Tage, `calibration.fit_ventilation_profile`) und nimmt das gemessene Lüften aus der
  Gebäudebilanz – `building.loss_w_per_k` bleibt der Verlust bei geschlossenen Fenstern, sonst zählte das Lüften
  doppelt (einmal im Verlust, einmal im Profil).
- Plan gegen Ist: Slots mit offenem Fenster (±30 min) werden beim Raum nicht verglichen.
- Empfehlung (`recommend`): je Tag ein Slot zwischen `recommend_start_hour` (11) und `recommend_end_hour` (15), der
  wärmste, bei Gleichstand der mit dem meisten PV-Überschuss; dort plant EOS `recommend_open_hours` (0,15) Lüften
  ein (abzüglich dessen, was das Profil in dem Fenster schon erwartet). Lösungsschlüssel `<id>_ventilate`.
- Kennzahl `kpi_airing_open_h_per_day` (gelerntes Lüften pro Tag).

`<id>-window-open` gehört in `device_measurement_history_keys`: Stoßlüften dauert oft kürzer als der
15-min-Takt der Läufe.

**HA** (angelegt 23.10.2026):

| Objekt | Zweck |
|---|---|
| `sensor.eos_fenster_offen_anteil` | Anteil offener Fenster der beheizten Räume (→ `wbb12-window-open`) |
| `script.eos_wp_raumsoll_setzen` | Feld `temperatur_og`: Sollwert für die Regler im Obergeschoss |
| `automation.eos_warmepumpe_heizmodus_umsetzen` | bei `sensor.eos_wbb12_zone_setback` = 1: OG auf min(Ziel, Absenktemperatur); nicht bei Feuchtewarnung |
| `sensor.eos_wp_sg_ready_soll` | Untergrenze je Raum: OG während der Absenkung Absenktemperatur − 1 K, sonst `input_number.eos_wp_absenk_untergrenze` (Start + Hysterese); Räume beim Lüften zählen nicht |
| `automation.eos_luftungsempfehlung` | Push „Jetzt lüften“ an Bewohner A und Bewohner B, wenn `sensor.eos_wbb12_ventilate` 1 wird (Heizbetrieb, kein Urlaub, Fenster zu, Schalter an) |
| `input_boolean.eos_luftungsempfehlung` | Empfehlung an/aus |
| `input_boolean.eos_og_absenkung_aktiv` | OG-Absenkung an/aus (aus = OG folgt dem Haus) |

EOS-Konfiguration (eingespielt 23.10.2026):

```json
"devices": {"heat_pumps": {"wbb12": {
  "zone_setback": {"enabled": true, "share": 0.4},
  "ventilation": {"enabled": true, "recommend": true}
}}},
"adapter": {"homeassistant": {
  "device_measurement_entity_ids": {
    "wbb12-setback-room-temperature": "number.wh_heizkreis2_raumsolltemperatur_absenk2",
    "wbb12-window-open": "sensor.eos_fenster_offen_anteil"
  },
  "device_measurement_history_keys": ["...", "wbb12-window-open"],
  "solution_entity_ids": ["...", "sensor.eos_wbb12_zone_setback", "sensor.eos_wbb12_ventilate"]
}}
```

## 3. §14a EnWG: Modul wählbar (keins, 1, 2, 1+3)

Einstellung `elecfee.grid_control_14a` (generisch, für jede Anlage):

| Modul | Wirkung in EOS |
|---|---|
| `none` | keine Preisänderung, keine Dimmung |
| `module1` | pauschale Kürzung – keine Wirkung auf die Optimierung; Dimmung aktiv |
| `module2` | steuerbares Gerät mit eigenem Zähler: Preis = Haushaltspreis − `module2_reduction_percent` × regulärer Netz-Arbeitspreis (inkl. MwSt.); das Gerät nutzt dann keinen PV-/Batteriestrom und zählt nicht zur Hausbilanz; Dimmung aktiv |
| `module1_3` | Modul 1 + zeitvariabler Netz-Arbeitspreis: EOS ersetzt im Festpreis-Gebührenprovider den regulären Netzpreis je Viertelstunde durch das Band (`module3_network_amt_kwh`, Fenster mit `months`); Dimmung aktiv |

Weitere Felder:

- `controllable_device_ids`: die steuerbaren Verbraucher, z. B. `wbb12`.
- `regular_network_amt_kwh`: der Netz-Arbeitspreis, der in `consumption_amt_kwh` steckt. Er wird für Modul 2 und Modul 3 gebraucht.
- `dimming_power_w`: Standard 4.200 W.
- `dimming_hold_hours`: Standard 1 h.

Ist der Messwert `<id>-power-limited` aktiv, begrenzt EOS die steuerbaren Verbraucher ab jetzt auf `dimming_power_w`.

```bash
# Modul 1+3 (MVV-Bänder als Standard), regulärer Netz-Arbeitspreis aus Preisblatt 2
python3 scripts/rl29/gridfee_14a.py --module module1_3 --device wbb12 \
  --regular-network-amt-kwh <EUR/kWh netto> --apply http://eos.local:8503
# Alternativ: --module none | module1 | module2
```

`consumption_amt_kwh` bleibt dabei unverändert (13,796 ct mit regulärem Netzpreis).

## 4. HA-Steuerungsschicht (angelegt 21.10., vervollständigt 22.10.2026)

HA übernimmt Mapping (Modus → Signal), Übertragungsweg und Sicherheitslogik. EOS liefert nur die Modi.

### Was je EOS-Modus geschaltet wird (Weg „Kontakte“, aktiv seit 22.10.)

| EOS-Modus | SG (Helfer, Standard) | WP | Räume (Label „EOS Raumregelung“) |
|---|---|---|---|
| 0 Reduziert | 1 | H2 aus + Heizkreis-Betriebsart **Absenk** (Modbus) | Raumsoll Normal (21 °C) |
| 1 Normal | 2 | H2 aus + **Automatik** | Raumsoll Normal |
| 2 Angehoben | 3 | **H2 ein** (SG-Anhebung +1 K) + Automatik | Raumsoll Angehoben (22 °C) |
| 3 Vorheizen | 3 | H2 ein + Automatik | Raumsoll Vorheizen (22 °C) |
| 4 Forciert | 4 | H2 ein + Betriebsart **Komfort** | Raumsoll Forciert (23 °C) |
| WW HEAT_PUMP | – | DE2 (Warmwasser PUSH) 60 min | – |
| WW ROD | – | noch nicht umgesetzt (Heizstab folgt) | – |

Weg „Modbus“: SG 1 → Absenk, 2 → Automatik, 3/4 → Komfort, H2 bleibt aus. H1 schaltet nur die
§14a-Sperre (MVV-Relais → KNX 9/1/0: H1 zu, H2 auf → Leistungsbegrenzung 4,2 kW); sie hat immer Vorrang
und geht als `wbb12-power-limited` an EOS.

Die Raumsollwerte werden nur gesetzt, wenn sich die Stufe ändert, und danach auf „Raumsoll Normal“
zurückgestellt. Damit entsprechen die Raumregler dem, was EOS rechnet (Angehoben: Sollwert + 1 K,
Vorheizen: Komfortmaximum, Forciert: Komfortmaximum + 1 K). Meldet ein KNX-Regler länger als
2 min einen anderen Sollwert zurück, setzt der 5-min-Takt ihn erneut (nur die abweichenden Regler).

### Heizkreis-Zwangsstellung (KNX)

`automation.eos_heizkreise_zwangsstellung` öffnet bei EOS-Modus Vorheizen/Forciert alle Heizkreise
über die KNX-Zwangsstellung (GA `3/4/X` je Raum, DPT Freigeben; im Wintermodus Ventil 100 %):

- Auswahl `input_select.eos_wp_zwangsstellung_bei`: „Vorheizen + Forciert“ / „nur Forciert“ / „aus“.
- Nur bei Winter (`binary_sensor.sommer_winter` aus, GA 8/0/5 = 0), Betriebsmodus Heizen,
  Hauptschalter an und umgesetztem SG-Ready-Ist ≥ 3 (damit gelten Pause, Datenalter und Hauptschalter).
- Ein Raum verlässt die Zwangsstellung bei „EOS WP Max Raumtemperatur“ (23,5 °C), Wiedereintritt 0,5 K darunter.
- Im Sommer wird nichts gesetzt; nur selbst gesetzte Kanäle werden freigegeben
  (`input_text.eos_wp_zwangsstellung_kanale`). KNX-Telegramme nur bei Änderung.
- Kanäle: 30 Multifunktionsraum, 40 Arbeitszimmer Bewohner A, 80 Flur EG, 90 HWR, 100 Küche, 110 Wohnzimmer,
  120 Gästezimmer, 130 Arbeitszimmer Bewohner B, 140 Bad EG, 150 Flur OG, 170 Schlafzimmer, 180 Kinderzimmer 1,
  190 Kinderzimmer 2, 200 Kinderzimmer 3, 210 Bad OG.
- Telegramme einzeln mit 300 ms Abstand; ist KNX nicht erreichbar (`binary_sensor.sommer_winter` unavailable),
  ändert die Automation nichts (sonst würde ein Verbindungsabbruch die Zwangsstellung flattern lassen).
  KNX-Integration mit Sendebegrenzung 20 Telegramme/s.
- Anzeige: Dashboard „Energie & Kosten“ → EOS Steuerung → Heizkreise.

### Sicherheitsregeln

| Regel | Wo | Einstellung |
|---|---|---|
| Nur im Betriebsmodus Haus „Heizen“ | Soll-Sensor | – |
| EOS-Plan fehlt oder älter als 45 min → SG 2 (neueste Meldung von `heating_mode`, `room_temperature_c` oder `dhw_mode`, `last_reported`) | Soll-Sensor | – |
| Keine Absenkung, wenn ein Raum unter der Untergrenze liegt, oder bei Feuchtewarnung. Start erst ab Untergrenze + Hysterese (gegen Pendeln), eine laufende Absenkung endet erst unter der Untergrenze. Räume mit offenem Fenster/Tür und bis zum Nachlauf nach dem Schließen zählen nicht (Fensterkontakte im selben Bereich wie der Regler, device_class window/door/opening) | Soll-Sensor | `input_number.eos_wp_absenk_untergrenze` (19,5 °C), `_absenk_hysterese` (0,5 K), `_luftung_nachlauf` (30 min) |
| Keine Anhebung ab wärmstem Raum ≥ Max | Soll-Sensor | `input_number.eos_wp_max_raumtemperatur` (23,5 °C) |
| Absenkung höchstens X min, danach gleich lange gesperrt | Automation + Soll-Sensor | `input_number.eos_wp_max_absenkdauer` (360 min) |
| Höchstens N Umschaltungen/Tag, danach nur Normal | Automation | `input_number.eos_wp_max_umschaltungen_pro_tag` (15) |
| Mindesthaltezeit (Rückkehr zu Normal immer sofort; 30 s Toleranz, damit der 5-min-Takt genau nach Ablauf schaltet) | Automation | `input_number.eos_wp_mindesthaltezeit` (55 min) |
| Manueller Eingriff an Betriebsart/H2 → Pause + Push | Automation | `input_number.eos_wp_pause_nach_eingriff` (240 min) |
| Höchstens N Warmwasserladungen/Tag | Automation | `input_number.eos_wp_max_warmwasserladungen_pro_tag` (2) |
| DE2 > 90 min zu → öffnen + Push | Überwachung | – |
| WP nicht im Heizbetrieb, Modbus weg, EOS-Plan fehlt, Tageslimit → Push | Überwachung | – |
| Notabschaltung (Hauptschalter aus): wärmster Raum ≥ 24 °C 15 min / kältester < 19 °C 30 min (ohne Räume beim Lüften: `sensor.raumtemperatur_kaltester_raum_ohne_luften`) | `automation.eos_warmepumpe_notabschaltung` | – |

**Betriebsmodus Haus „Standby“** (ab 24.10.2026): `input_select.betriebsmodus_haus` hat zusätzlich `Standby`.
`automation.heizung_betriebsmodus_ruckmelden` setzt es, wenn die WP-Systembetriebsart auf `sys_operationmode_standby` geht
(z. B. Urlaubsmodus); `automation.heizung_betriebsmodus_umsetzen_knx_warmepumpe` schaltet bei Auswahl Standby die WP in Standby
(Frostschutz), KNX Sommer/Winter bleibt. Bei Standby rechnet die EOS-Steuerung nicht (Soll-Sensor → SG 2), Zwangsstellung und
Lüftungsempfehlung sind aus.

**Urlaubsmodus** (geändert 24.10.2026): Beim Einschalten bricht `automation.urlaubsmodus_wp_standby_wlan_gerate` sofort alle
laufenden WP-Aktionen ab (Warmwasser-Skripte, DE2, H2) und schaltet die WP direkt in Standby. EOS-Pläne holen die WP im Urlaub
nicht mehr aus dem Standby; zurück nur bei Raum unter `input_number.urlaub_standby_untergrenze_raum`, zum Wiederanlauf vor der
Rückkehr oder am Urlaubsende. `automation.eos_warmepumpe_warmwasser_laden` startet im Urlaub keine Ladung. Geht die WP nach dem
Urlaub zurück auf Automatik, setzt das Rückmelden den Betriebsmodus Haus von Standby auf Heizen (die WP bleibt in Automatik).

**Feuchteschutz** (`binary_sensor.feuchte_warnung_wohnraume`, Template, ab 23.10.2026): sperrt Absenkung und OG-Absenkung.
An, wenn `binary_sensor.feuchte_wohnraume_hoch` (Max. Arbeitszimmer-Sensoren > 63 %, aus < 58 %) seit `input_number.feuchteschutz_verzogerung_wohnraume` (60 min)
an ist, oder `binary_sensor.feuchte_bader_hoch` (Max. Bad OG/Bad EG > 70 %, aus < 65 %) seit `input_number.feuchteschutz_verzogerung_bader` (120 min).
Kurzes Duschen löst damit nichts mehr aus; Werkstatt und `meter_7d57` zählen nicht mehr. `automation.bad_luften_erinnerung_nach_dem_duschen`:
Push an Bewohner A und Bewohner B, wenn ein Bad 30 min über 70 % liegt (höchstens einmal pro Stunde).

**Temporär (wieder entfernen nach Einbau der neuen KNX-Spannungsversorgung):** `automation.knx_raumregler_aus_standby_auf_komfort_temporar` – fällt ein Raumregler mit Label `eos_raumregelung` 1 min in den Standby (MDT-Aktoren nach Busspannungseinbruch), sendet sie Komfort (1) auf die gemeinsame Betriebsart-GA 3/3/100; zusätzlich Prüfung beim HA-Start und alle 30 min, danach 5 min Sperre.

### Objekte

| Objekt | Zweck |
|---|---|
| `input_boolean.eos_wp_steuerung_aktiv` | Hauptschalter; beim Ausschalten: H2 aus, Automatik, Räume Normal |
| `input_select.eos_wp_ubertragungsweg` | `Kontakte` (seit 22.10.) oder `Modbus` |
| `input_select.eos_wp_sg_ready_bei_*` | Mapping Heizmodus → SG (1 / 2 / 3 / 3 / 4) |
| `input_number.eos_wp_raumsoll_*` | Raumsoll Normal / Angehoben / Vorheizen / Forciert (21 / 22 / 22 / 23 °C) |
| `sensor.eos_wp_sg_ready_soll` | SG-Soll mit den Regeln des Soll-Sensors |
| `sensor.raumtemperatur_warmster_raum` / `_kaltester_raum` | Grenze für Anhebung / Anzeige kältester Raum |
| `sensor.raumtemperatur_kaltester_raum_ohne_luften` | kältester Raum ohne lüftende Räume (Notabschaltung) |
| `input_number.eos_wp_sg_ready_ist`, `input_number.eos_wp_raumsoll_ist` | zuletzt umgesetzter Zustand |
| `counter.eos_wp_umschaltungen_heute`, `counter.eos_wp_warmwasserladungen_heute` | Tageszähler (Reset 0:00) |
| `input_datetime.eos_wp_letzte_umschaltung`, `_absenkung_seit`, `_absenkung_gesperrt_bis`, `_pause_bis` | Zeitstempel der Regeln |
| `automation.eos_warmepumpe_heizmodus_umsetzen` | setzt Soll um (2 min entprellt, Takt 5 min) |
| `automation.eos_warmepumpe_warmwasser_laden` | WW-Ladung, wenn EOS HEAT_PUMP plant |
| `automation.eos_warmepumpe_manueller_eingriff_erkennen` | Pause bei Fremdänderung |
| `automation.eos_warmepumpe_uberwachung` | Zähler-Reset, DE2-Watchdog, Meldungen |
| `script.eos_wp_sg_ready_kontakte` / `_modbus` | Übertragungswege |
| `script.eos_wp_raumsoll_setzen` | Raumregler mit Label „EOS Raumregelung“ setzen |
| `script.eos_wp_warmwasser_laden_kontakte` / `_modbus` | WW-Ladung je Weg |
| KNX-Treppenlicht DE2 (3600 s) / H2 (90 min), `automation.eos_h2_lebenszeichen` | Zeitlogik im Aktor (nachtriggerbar, ohne Vorwarnung, nach Busspannungswiederkehr aus): DE2 öffnet nach 60 min, H2 nach 90 min von selbst, auch wenn HA ausfällt. Das WW-Skript triggert bei > 60 min nach; bei SG-Ist ≥ 3 schaltet die Lebenszeichen-Automation H2 alle 30 min erneut ein |
| `automation.eos_warmwasser_push_beenden`, `input_datetime.eos_wp_warmwasser_push_bis` | Absicherung: DE2-Push endet zur gemerkten Endzeit, auch wenn das Skript abgebrochen wurde (Neustart/Reload); manueller Push ohne EOS bleibt unberührt |
| `binary_sensor.wp_sg1_h1_status` (KNX 9/1/11) | Status H1 |
| `switch.wp_sg2_h2_anhebung` (KNX 9/1/20/21) | H2 = SG Ready „Erhöhter Betrieb“ |
| `switch.wp_warmwasser_push_de2` (KNX 9/1/30/31) | DE2 = Warmwasser PUSH |

Eingänge getestet am 22.10.2026: H1 (Leistungsbegrenzung), H2, DE2, §14a-Sperre. DE1 (Taupunkt) nicht getestet.

### Inbetriebnahme: stündliche Plan-Prüfung

Nach dem Einschalten des Hauptschalters wird der Plan stündlich geprüft:

```bash
python /data/rl29/check_heatpump_plan.py --url http://localhost:8503   # im EOS-Container
python3 scripts/rl29/check_heatpump_plan.py --url http://eos.local:8503
```

Exit 0 = plausibel, 1 = Warnung, 2 = unplausibel → Hauptschalter aus. Geprüft werden Planalter, geplante
Raumtemperaturen gegen das Komfortband, Dauer aktiver Modi, Anhebung ohne PV-Überschuss zu Spitzenpreisen,
lange Absenkung und Anzahl der Warmwasserladungen. Unplausibel sind > 6 h Vorheizen/Forciert am Stück
(`--max-active-h`); Angehoben (+1 K, keine Zwangsstellung) gibt erst ab > 8 h aktiv am Stück eine Warnung
(`--max-raised-h`), Absenkung ab > 6 h (`--max-reduced-h`, wie `input_number.eos_wp_max_absenkdauer`).

### Kühlen (Fork, ab 22.10.2026)

`devices.heat_pumps.<id>.cooling` (`enabled`, `setpoint_c` 24, `max_c` 26, `max_cool_w`, `eer`, `min_flow_c` 18,
`dewpoint_margin_k` 2, `pv_only`, `min_pv_surplus_w` 500). `eer` ist der Nennwert bei `eer_rated_outdoor_c`
(35 °C) / `eer_rated_flow_c` (18 °C); je Slot wird er mit dem Carnot-Wirkungsgrad auf Außentemperatur und
tatsächlichen Kühlvorlauf umgerechnet (heißer Nachmittag schlechter, milder Abend besser; `eer_constant: true`
schaltet das ab). Meldet HA `<id>-cooling-season` = 1, wählt EOS je Stunde
zwischen NORMAL (nicht aktiv kühlen) und COOL (Heizmodus 5). Mit `pv_only` darf Kühlen höchstens den
prognostizierten PV-Überschuss der Stunde nutzen (darunter `min_pv_surplus_w`: gar nicht). Die Vorlauftemperatur
bleibt ≥ max(`min_flow_c`, Taupunkt `<id>-dewpoint` + Marge). Über `max_c` greift die Überhitzungsstrafe.
HA setzt COOL in die Freigabe von Heizkreis 1 (Kühlkreis) um.

### Später

- **Heizstab** (in ca. 2 Wochen): `dhw.rod_power_w`, `dhw_modes` [0, 1, 2], HA-Umsetzung ROD über my-PV (Modbus).

## 5. Stack: HA direkt statt Bridge (Vorschlag)

Diese Änderung macht Bewohner A selbst im Portainer-UI, weil `HA_TOKEN` dort gesetzt ist.

- `eos-ha-bridge` entfernen.
- `SUPERVISOR_TOKEN` am EOS-Container entfernen.
- Hinzufügen:

```yaml
    environment:
      EOS_ADAPTER__HOMEASSISTANT__BASE_URL: http://homeassistant.local:8123
      EOS_ADAPTER__HOMEASSISTANT__TOKEN: ${HA_TOKEN}
```

Danach prüfen:

```bash
curl -s http://eos.local:8503/v1/config/adapter/homeassistant/homeassistant_entity_ids | head -c 300
```

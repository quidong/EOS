# RL29-Fork: Backlog Optimierung

Offene Themen für den EOS-Fork und die Anlage, so beschrieben, dass sie in einem neuen Chat ohne
Vorgeschichte bearbeitet werden können. Stand 22.10.2026, nach dem Umbau der Kalibrierung.

## Arbeitsregeln (gelten für jedes Thema)

- Repo `github.com/quidong/eos`, Arbeitsbranch **`rl29/main`** (alles direkt dorthin, keine Review-Branches;
  `main` ist nur Upstream-Spiegel). Jeder Push baut und veröffentlicht das Image (`.github/workflows/rl29-image.yml`);
  reine Doku-Commits mit `[skip ci]`, sonst bricht ein laufender CI-Lauf ab.
- Fork-Code bleibt in eigenen Dateien oder kurz markierten Blöcken („RL29 fork“), damit das Rebase auf Upstream
  konfliktfrei bleibt (siehe `RL29.md`).
- Vor dem Push: `uv sync --extra dev`, `uv run pytest -q tests/rl29`, `ruff check`, `ruff format`, `mypy` auf
  die geänderten Dateien; neue Funktionen bekommen Tests in `tests/rl29/` nach dem Muster der vorhandenen.
  Config-Felder geändert → `make gen-docs` (bzw. die drei `scripts/generate_*.py`) und `openapi.json`,
  `docs/_generated/*` mit committen.
- Betrieb: EOS läuft als Container `eos` auf der VM „energy“ (192.168.30.85:8503, Portainer-Stack `eos`),
  Home Assistant auf 192.168.30.180. Config-Änderungen über `PUT /v1/config/<pfad>` + `PUT /v1/config/file`,
  vorher `GET /v1/config` als Backup sichern. Änderungen in HA nur nach Absprache.
- Dokumentation der Anlage: `RL29.md` (Fork, Deploy), `RL29-HEATPUMP.md` (Wärmepumpe, Kalibrierung 2a,
  Plan-Ist 2b, HA-Steuerung), `RL29-APPLIANCES.md` (Waschmaschine).
- Datenordner im Container `/data`: `/data/rl29/heatpump_calibration.json`, `/data/rl29/heatpump_plan_actual.json`,
  `/data/rl29/appliance_profiles.json`, Config `/data/config/EOS.config.json`.

## Erledigt (zur Einordnung)

- Automatische Kalibrierung des WP-Modells aus Messwerten (Gebäude mit Sonne, COP, Speicherverlust),
  `devices/thermal/calibration.py`, `devices/heatpumpcalibration.py`.
- Plan gegen Ist in EOS, `devices/heatpumpplanactual.py`.
- Endwert gespeicherter Wärme nach günstigen Stunden/PV-Überschuss, Kühl-EER nach Außentemperatur.
- Waschmaschinen-Profile: Dauer-Quantil, Mischen ohne Nullen, Ausreißerschutz.
- Konfiguration: PV-Prognose `PVForecastAkkudoktor` (backend local) mit Kalibrierung, `prediction.hours` 72,
  Legionellen aus (bis Heizstab), Einspeisegebühren 0, Temperaturkoeffizient −0,29 %/K.

## Offen

### B1. Warmwasser-Zapfprofil lernen

- **Ziel:** `dhw.draw_profile_wh` (24 Stundenwerte, heute Standard 4 kWh/Tag) durch ein aus Messwerten gelerntes
  Profil ersetzen, getrennt Werktag/Wochenende.
- **Warum:** Die WW-Ladung wird vor die erwarteten Spitzen gelegt; stimmt das Profil nicht, lädt EOS zur falschen Zeit
  oder zu wenig. Duschen ist unregelmäßig, aber morgens/abends gehäuft; deshalb je Stunde ein **oberes Quantil**
  (z. B. 0,7) über die letzten 3–4 Wochen, nicht der Mittelwert (zu viel kostet nur eine frühere Ladung, zu wenig die
  kalte Dusche). Den Nachschub regelt ohnehin die Hysterese der WP.
- **Ansatz:** Zapfung je Stunde aus dem Temperaturabfall des Speichers (`<id>-dhw-temperature`, C = 221 Wh/K)
  abzüglich Standby-Verlust (`dhw.loss_w_per_k`), Ladestunden (Anstieg, `<id>-dhw-heat-power` oder geschätzt wie in
  `estimate_dhw_heat`) ausnehmen. Gleiche Mechanik wie die Kalibrierung: rollierendes Fenster, Prior = konfiguriertes
  Profil, Ergebnis im Kalibrierungs-JSON, Anwendung über `HeatPumpCommonSettings.calibrated()`.
- **Dateien:** `devices/thermal/calibration.py` (neuer Fit), `devices/heatpumpcalibration.py`,
  `devices/settings/heatpumpsettings.py` (`calibration.fit_draw_profile`, Quantil), Tests `tests/rl29/test_heatpump_calibration*.py`.
- **Fertig wenn:** Nach 14 Tagen Historie steht im Log ein gelerntes Profil mit Tages-kWh; Plan-Ist (`lead 6h`) zeigt
  beim Speicher keinen systematischen Bias mehr.
- **Voraussetzung:** 2 Wochen Messwerte nach dem 22.10.2026. Erst sinnvoll, wenn Plan-Ist (erledigt, `RL29-HEATPUMP.md` 2b) zeigt, dass Warmwasser
  überhaupt eine Fehlerquelle ist.
- **Aufwand:** klein (½ Tag).

### B2. Lastprognose aus eigener Historie

- **Ziel:** Provider, der das Haushaltsprofil (ohne WP) aus den eigenen Messwerten bildet: Wochentag × Stunde,
  gleitend über die letzten 4–8 Wochen, statt des korrigierten Standardprofils `LoadAkkudoktorAdjusted`.
- **Warum:** Zwei Homeoffice-Arbeitsplätze, Waschmaschine, Kühlung – das Standardprofil passt strukturell nicht; der
  Lastfehler ist nach der PV-Prognose der größte Planfehler.
- **Ansatz:** Neuer Load-Provider `LoadMeasured` (Fork-Datei `prediction/loadmeasured.py`) auf Basis
  `measurement.load_emr_keys` (`sensor.eos_grundlast_gesamt`, kWh kumuliert; die WP ist dort schon herausgerechnet).
  Je (Wochentag, Stunde) Median oder EWMA der letzten N Wochen, Fallback auf `LoadAkkudoktorAdjusted`, solange weniger
  als 2 Wochen Daten. Feiertage wie Sonntag.
- **Fertig wenn:** MAE der Lastprognose (Plan-Ist-Auswertung um eine Last-Kennzahl erweitern, siehe `RL29-HEATPUMP.md` 2b) sinkt gegen
  `LoadAkkudoktorAdjusted` über eine Woche.
- **Voraussetzung:** 4 Wochen Messung (ab ca. 20.11.2026). KNX-Zähler-Plausibilität (Hausverbrauch vs. WP-Zähler) muss
  geklärt sein, siehe Hinweis in `RL29-HEATPUMP.md` Abschnitt 2.
- **Aufwand:** mittel (1 Tag).

### B3. Optimierer-Qualität messen und einstellen

- **Ziel:** Wissen, ob 300 Individuen × 400 Generationen (ca. 40 s) den Suchraum ausreichend ausschöpfen.
- **Warum:** Das Log zeigt alle ~20 Generationen „soft restart“ wegen Stagnation; unklar, ob das Konvergenz oder
  Verfangen in lokalen Optima ist. Ein Lauf darf bis zu 15 Minuten dauern (Intervall 900 s).
- **Ansatz:** Offline-Experiment mit `scripts/rl29/eos_snapshot.py` oder direkt der Request-JSON eines realen Laufs
  (`GET /v1/energy-management/optimization/solution` liefert die Eingangsdaten nicht; die Konfiguration plus
  Prognosen aus `GET /v1/prediction/...` nachbauen oder einen Test wie `tests/rl29/test_heatpump_optimization.py::test_joint_optimization_beats_status_quo`
  mit festem Seed verwenden): Fitness bei 400 vs. 2000 Generationen, 300 vs. 1000 Individuen, 3 Seeds. Ist die
  Fitness bei mehr Rechenzeit deutlich besser (> 2 %), `optimization.genetic.generations`/`individuals` erhöhen; sonst
  die Operatoren ansehen (WP-Gene werden von `heatpumpgenes.py` gesät: `pv_led_seed`, `price_led_seed`).
- **Fertig wenn:** Eine kurze Tabelle Fitness × Laufzeit vorliegt und die Einstellung begründet gewählt ist.
- **Aufwand:** klein bis mittel.

### B4. Komfortband als Speicher-Stellhebel

- **Ziel:** Entscheiden, ob `comfort.max_c` (22 °C) an sonnigen Tagen höher darf (22,5), damit mehr PV-Wärme in den
  Estrich geht.
- **Warum:** Mehr Band = mehr Speicher, aber Komfortfrage (alle Räume einheitlich 21 °C ist Vorgabe).
- **Ansatz:** Erst mit Plan-Ist prüfen, ob die Räume bei PREHEAT/RAISED überhaupt die geplanten 22 °C erreichen (HA
  setzt Raumsoll „Vorheizen“ 22 °C, „Forciert“ 23 °C, KNX-Zwangsstellung). Dann ggf. `comfort.max_c` 22,5 und
  `overheat_penalty_amt_per_kh` prüfen; Raumsoll-Helfer in HA mitziehen.
- **Voraussetzung:** Plan-Ist liefert Zahlen; Entscheidung des Hausherrn.
- **Aufwand:** klein.

### B5. Plan-Ist-Kennzahlen nach Home Assistant

- **Ziel:** Die Werte aus `/data/rl29/heatpump_plan_actual.json` (Bias/MAE Raum, Energieverhältnis, lead 1h/6h) als
  Sensoren in HA, fürs Dashboard „Energie & Kosten“.
- **Ansatz:** Als Lösungsschlüssel in die Solution aufnehmen (`geneticsolution.py`, Schlüssel `<id>_plan_room_bias_k`
  usw.) und in `adapter.homeassistant.solution_entity_ids` eintragen; oder einfacher: eigener kleiner Endpunkt
  `GET /v1/rl29/heatpump/plan-actual` und in HA ein REST-Sensor (HA-Änderung absprechen).
- **Aufwand:** klein.

### B6. Heizstab (my-PV 3 kW) in Betrieb nehmen

- **Ziel:** EOS plant den Heizstab im Trinkwasserspeicher (ROD-Modi bis 70 °C, Legionellenlauf über den Heizstab,
  Überschuss in Warmwasser), HA setzt ihn über my-PV (Modbus über den Proxy) um.
- **Vorbereitung in EOS (geht jetzt schon, ohne Hardware):**
  - Config nach Einbau: `dhw.rod_power_w: 3000`, `dhw.rod_targets_c: [60, 70]`, `optimization.dhw_modes: [0, 1, 2]`,
    `dhw.legionella_interval_days: 7` (heute `null`, weil ohne Heizstab 60 °C unerreichbar sind; `unit.max_dhw_c` 55).
  - Messwerte: `<id>-dhw-heat-power` kann aus der my-PV-Leistung gespeist werden (beim Heizstab ist Strom = Wärme);
    damit rechnet die Kalibrierung die Heizstab-Wärme korrekt aus der Gebäudebilanz heraus. Der Heizstab-Strom muss
    **aus der Grundlast heraus** (siehe B13), sonst zählt EOS ihn doppelt.
  - Der Endwert bewertet Speicherwärme über dem WP-Sollwert mit Verlustabschlag (`heatpumpgenes.py`,
    `TANK_DWELL_HOURS` = 12 h). Prüfen, ob 12 h zum Zapfprofil passt (B1).
- **HA-Umsetzung (absprechen):** Lösungsschlüssel `sensor.eos_wbb12_dhw_mode` (2 = ROD) und
  `sensor.eos_wbb12_rod_target_c` → Script `script.eos_wp_heizstab` setzt my-PV-Leistung/Solltemperatur; Abschaltung
  bei Zieltemperatur erreicht oder Modus ≠ ROD; Sicherheitsregeln wie bei der WW-Ladung (Tageslimit, Pause bei
  Eingriff, Push). Vorsicht: Heizstab und WP-Ladung können gleichzeitig laufen (Modell erlaubt das), die WP-Regelung
  kennt den Heizstab nicht; die WP soll hohe Temperaturen meiden (Legionellen nur über Heizstab).
- **Prüfung nach zwei Wochen:** Plan-Ist (erledigt, `RL29-HEATPUMP.md` 2b) für den Speicher; im Log `dhw_mode` ROD nur in Stunden mit
  PV-Überschuss oder Negativpreis; Legionellenlauf alle 7 Tage protokolliert (`legionella_ok`).
- **Voraussetzung:** Hardware (bestellt, Oktober 2026).
- **Aufwand:** EOS klein (Config + Messwert), HA mittel.

### B7. Batterie (BYD Battery-Box HVM 11 kWh) in Betrieb nehmen

- **Ziel:** Batterie in EOS einbinden und gemeinsam mit WP und Heizstab optimieren lassen: Überschuss geht dorthin,
  wo er am meisten wert ist (Batterie ≈ ersetzt Netzstrom 1:1 abzüglich Wirkungsgrad; Heizstab ersetzt
  WP-Strom/COP; Estrich ersetzt spätere WP-Stunden). Das ergibt sich aus der Fitness, wenn die Bewertungen stimmen.
- **Vorbereitung in EOS:**
  - `devices.batteries.<id>`: `capacity_wh` (Datenblatt BYD HVM 11, nutzbar), `min_soc_percentage`/`max_soc_percentage`,
    `max_charge_power_w`/`max_discharge_power_w` (Grenze des Verto 20 für diese Batterie), `charging_efficiency`/
    `discharging_efficiency`, `levelized_cost_of_storage_amt_kwh` (Zyklenkosten, sonst zykelt EOS die Batterie
    für Centbeträge); `devices.inverters.fronius.battery_id` setzen; `max_batteries: 1`.
  - Messwert `<batterie-id>-soc-factor` aus HA (Fronius SunSpec Modbus, SoC 0–1) in
    `adapter.homeassistant.device_measurement_entity_ids`; Lösungsschlüssel `..._charge_power_w`/Modi in
    `solution_entity_ids` für die HA-Umsetzung (Fronius Batteriesteuerung über Modbus: Ladefreigabe/Netzladen,
    absprechen).
  - Grundlast: Batterie-Laden/-Entladen darf nicht in `sensor.eos_grundlast_gesamt` stecken (B13); Fronius sitzt vor
    dem Zähler und sieht alle Flüsse, daraus den reinen Hausverbrauch bilden.
  - `optimization.genetic.terminal_value_mode` ist `AUTO`: prüfen, wie der Batterie-Endwert bestimmt wird, und gegen
    den Wärme-Endwert (`stored_heat_price_quantile`) abgleichen, sonst bevorzugt EOS systematisch einen Speicher.
  - §14a: Ein Speicher ≥ 4,2 kW ist eine steuerbare Verbrauchseinrichtung; bei MVV prüfen, ob die Dimmung auch für
    die Batterie gilt. Falls ja, `elecfee.grid_control_14a.controllable_device_ids` um die Batterie erweitern und die
    Dimmung in EOS auf Batterie-Netzladen anwenden (Fork-Erweiterung, heute nur Wärmepumpe).
  - Kühlen nur mit PV (`cooling.pv_only`): `pv_surplus_w` in `build_run_plan` ist PV minus Last; mit Batterie
    entscheiden, ob Batterie-Entladung als „Solarstrom“ zählt (Vorgabe: ausschließlich Solarstrom).
- **Prüfung nach einer Woche:** Plan-Ist für Batterie (SoC-Verlauf Plan/Ist; Erweiterung von
  `devices/heatpumpplanactual.py` um die Batterie), Eigenverbrauchsquote und Netzbezug in den Abendstunden.
- **Voraussetzung:** Hardware (kommt später); Fronius-Batteriesteuerung in HA.
- **Aufwand:** EOS mittel (Config, §14a-Erweiterung, Plan-Ist), HA mittel.

### B13. Grundlast-Sensor: Heizstab und Batterie herausrechnen

- **Ziel:** `sensor.eos_grundlast_gesamt` (kWh, Quelle für `measurement.load_emr_keys` und damit die Lastprognose)
  enthält genau den Verbrauch, den EOS **nicht** selbst plant: Hausverbrauch ohne Wärmepumpe, ohne Heizstab, ohne
  Batterieladung, ohne Waschmaschine (die ist bei Fernstart-Planung selbst geplant; ohne Fernstart gehört sie in die
  Grundlast).
- **Warum:** Alles, was EOS plant und zusätzlich in der Grundlast steckt, wird doppelt gezählt; die Prognose wird zu
  hoch, PV-Überschuss zu niedrig, der Plan zu konservativ.
- **Ansatz (HA, absprechen):** Template-Sensor aus Fronius-Hausverbrauch minus KNX-Zähler Wärmepumpe
  (`sensor.energie_warmepumpe`) minus my-PV-Zähler minus Batterieladung (Fronius), monoton steigend (`total_increasing`).
  Nach jeder Änderung die EOS-Lasthistorie prüfen (`LoadAkkudoktorAdjusted` korrigiert aus den letzten Wochen).
- **Fertig wenn:** Plan-Ist Energie (Haushalt) über eine Woche Verhältnis ≈ 1.

### B8. Dynamischer Tarif (ostrom) umstellen

- **Ziel:** Beim Wechsel von LichtBlick (fix) auf ostrom Simply Dynamic die Preisbildung in EOS prüfen:
  `elecprice` EnergyCharts (Day-Ahead) + `elecfee.elecfeefixed.consumption_amt_kwh` (heute 13,796 ct inkl. regulärem
  Netzpreis) + §14a Modul 1+3 (`elecfee.grid_control_14a`, Bänder in `scripts/rl29/gridfee_14a.py`). ostrom: Börse
  + 2,49 ct netto, stündlich. MwSt-Behandlung (`consumption_percent_amt` 19 %) kontrollieren.
- **Fertig wenn:** Preis je Stunde in EOS = Rechnungspreis ostrom (Stichprobe über einen Tag).
- **Voraussetzung:** Vertragsbeginn.

### B9. Wetterprognose: lokale Modelle

- **Ziel:** `pvforecast.akkudoktor.weather_models` von `best_match` auf `["icon_d2", "icon_eu"]` (DWD, feiner für
  Deutschland) stellen und über zwei Wochen den PV-Kalibrierungs-MAE im Log vergleichen.
- **Aufwand:** sehr klein, nur Config. Vorher/nachher-Vergleich über die Logzeile
  `PVForecastAkkudoktorLocal calibration ... MAE`.

### B10. PV-Kalibrierung beobachten

- Nach 3–4 Wochen den `global factor` im Log prüfen (bifaziale Glas-Glas-Module: eher > 1 erwartet). Bei
  dauerhaft > 1,3 den `albedo` (0,25) und `inverter_efficiency` (0,96) in der Config anpassen, damit die
  Kalibrierung nicht am Rand ihres Bandes (`calibration_max_factor` 1,5) läuft.

### B11. Upstream-Rebase

- Regelmäßig (monatlich) `rl29/main` auf `Akkudoktor-EOS/EOS` main rebasen (`main` im Fork spiegeln, dann
  `git rebase`), Fork-Tests laufen lassen. Fork-Dateien sind in `RL29.md` aufgelistet; Konfliktkandidaten sind
  `configrequest.py`, `genetic.py`, `homeassistant.py`.

### B12. Kühlsaison vorbereiten (Frühjahr 2027)

- `cooling.enabled`, Messwerte `<id>-cooling-season` und `<id>-dewpoint` (in HA vorhanden), HA-Umsetzung COOL auf
  Heizkreis 1. Mit der neuen EER-Kurve (`EerModel`) prüfen, ob die Nennwerte (`eer` 4,5 bei 35/18 °C) zur WBB 12
  passen (Datenblatt Kühlen), sonst `eer`/`eer_rated_*` setzen. Erste Kalibrierung des Kühlbetriebs ist offen
  (Fit der EER aus Messwerten analog `fit_cop_scale`).

## Nicht geplant

- Estrich-Temperatursensor: es gibt keinen; der Estrich bleibt Modellgröße (Rücklauf als Näherung wurde verworfen).
- PDF-Bericht des Optimierers: nicht gebraucht.

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
- der Wert der am Horizontende gespeicherten Wärme.

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
- **Gebäudeparameter** kommen aus `scripts/rl29/calibrate_heatpump.py`. Die Defaults sind Schätzwerte.
- Ein **Estrich-Messwert** existiert nicht. EOS schätzt ihn dann aus dem Gleichgewicht.

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
  --regular-network-amt-kwh <EUR/kWh netto> --apply http://192.168.30.85:8503
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
Vorheizen: Komfortmaximum, Forciert: Komfortmaximum + 1 K).

### Sicherheitsregeln

| Regel | Wo | Einstellung |
|---|---|---|
| Nur im Betriebsmodus Haus „Heizen“ | Soll-Sensor | – |
| EOS-Plan fehlt oder älter als 45 min → SG 2 | Soll-Sensor | – |
| Keine Absenkung bei kältestem Raum < 19,5 °C oder Feuchtewarnung | Soll-Sensor | – |
| Keine Anhebung ab wärmstem Raum ≥ Max | Soll-Sensor | `input_number.eos_wp_max_raumtemperatur` (23,5 °C) |
| Absenkung höchstens X min, danach gleich lange gesperrt | Automation + Soll-Sensor | `input_number.eos_wp_max_absenkdauer` (180 min) |
| Höchstens N Umschaltungen/Tag, danach nur Normal | Automation | `input_number.eos_wp_max_umschaltungen_pro_tag` (12) |
| Mindesthaltezeit (Rückkehr zu Normal immer sofort) | Automation | `input_number.eos_wp_mindesthaltezeit` (55 min) |
| Manueller Eingriff an Betriebsart/H2 → Pause + Push | Automation | `input_number.eos_wp_pause_nach_eingriff` (240 min) |
| Höchstens N Warmwasserladungen/Tag | Automation | `input_number.eos_wp_max_warmwasserladungen_pro_tag` (2) |
| DE2 > 90 min zu → öffnen + Push | Überwachung | – |
| WP nicht im Heizbetrieb, Modbus weg, EOS-Plan fehlt, Tageslimit → Push | Überwachung | – |

### Objekte

| Objekt | Zweck |
|---|---|
| `input_boolean.eos_wp_steuerung_aktiv` | Hauptschalter; beim Ausschalten: H2 aus, Automatik, Räume Normal |
| `input_select.eos_wp_ubertragungsweg` | `Kontakte` (seit 22.10.) oder `Modbus` |
| `input_select.eos_wp_sg_ready_bei_*` | Mapping Heizmodus → SG (1 / 2 / 3 / 3 / 4) |
| `input_number.eos_wp_raumsoll_*` | Raumsoll Normal / Angehoben / Vorheizen / Forciert (21 / 22 / 22 / 23 °C) |
| `sensor.eos_wp_sg_ready_soll` | SG-Soll mit den Regeln des Soll-Sensors |
| `sensor.raumtemperatur_warmster_raum` / `_kaltester_raum` | Grenzen für Anhebung / Absenkung |
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
| `binary_sensor.wp_sg1_h1_status` (KNX 9/1/11) | Status H1 |
| `switch.wp_sg2_h2_anhebung` (KNX 9/1/20/21) | H2 = SG Ready „Erhöhter Betrieb“ |
| `switch.wp_warmwasser_push_de2` (KNX 9/1/30/31) | DE2 = Warmwasser PUSH |

Eingänge getestet am 22.10.2026: H1 (Leistungsbegrenzung), H2, DE2, §14a-Sperre. DE1 (Taupunkt) nicht getestet.

### Inbetriebnahme: stündliche Plan-Prüfung

Nach dem Einschalten des Hauptschalters wird der Plan stündlich geprüft:

```bash
python /data/rl29/check_heatpump_plan.py --url http://localhost:8503   # im EOS-Container
python3 scripts/rl29/check_heatpump_plan.py --url http://192.168.30.85:8503
```

Exit 0 = plausibel, 1 = Warnung, 2 = unplausibel → Hauptschalter aus. Geprüft werden Planalter, geplante
Raumtemperaturen gegen das Komfortband, Dauer aktiver Modi, Anhebung ohne PV-Überschuss zu Spitzenpreisen,
lange Absenkung und Anzahl der Warmwasserladungen.

### Später

- **Heizstab** (in ca. 2 Wochen): `dhw.rod_power_w`, `dhw_modes` [0, 1, 2], HA-Umsetzung ROD über my-PV (Modbus).
- **Kühlen**: EOS plant Kühlen mit; Bedingung: Kühlen nur aus PV-Überschuss (kein Netz). Braucht Kühlmodus im
  Wärmemodell, Taupunktgrenze (DE1/KNX 3/5/x) und eine Strombilanz-Nebenbedingung je Stunde.

## 5. Stack: HA direkt statt Bridge (Vorschlag)

Diese Änderung macht Steffen selbst im Portainer-UI, weil `HA_TOKEN` dort gesetzt ist.

- `eos-ha-bridge` entfernen.
- `SUPERVISOR_TOKEN` am EOS-Container entfernen.
- Hinzufügen:

```yaml
    environment:
      EOS_ADAPTER__HOMEASSISTANT__BASE_URL: http://192.168.30.180:8123
      EOS_ADAPTER__HOMEASSISTANT__TOKEN: ${HA_TOKEN}
```

Danach prüfen:

```bash
curl -s http://192.168.30.85:8503/v1/config/adapter/homeassistant/homeassistant_entity_ids | head -c 300
```

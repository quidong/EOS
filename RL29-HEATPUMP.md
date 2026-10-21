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

## 4. HA-Steuerungsschicht (angelegt am 21.10.2026)

HA übernimmt das Mapping (Modus → Signal) und den Übertragungsweg. Angelegt ist Folgendes:

| Objekt | Zweck |
|---|---|
| `input_boolean.eos_wp_steuerung_aktiv` | Hauptschalter (Standard **aus**); beim Ausschalten setzt HA den Normalbetrieb zurück |
| `input_select.eos_wp_ubertragungsweg` | `Modbus` (jetzt) oder `Kontakte` (nach der Verdrahtung) |
| `input_select.eos_wp_sg_ready_bei_reduziert` / `_normal` / `_angehoben` / `_vorheizen` / `_forciert` | Mapping Heizmodus → SG-Ready-Zustand (Standard 1 / 2 / 3 / 3 / 4) |
| `sensor.eos_wp_sg_ready_soll` (Template-Helfer) | SG-Ready-Soll aus EOS-Modus und Mapping, mit Sicherheitsregeln: nur bei Betriebsmodus „Heizen“; keine Absenkung, wenn der kälteste Raum unter 19,5 °C liegt oder die Feuchtewarnung aktiv ist; ohne EOS-Wert gilt 2 |
| `script.eos_wp_sg_ready_modbus` | Modbus-Weg: SG 1 → Heizkreis-Betriebsart Absenk, 2 → Automatik, 3/4 → Komfort (`select.wh_heizkreis2_betriebsart2`); schreibt nur bei einer Änderung |
| `script.eos_wp_warmwasser_laden_modbus` | Modbus-Weg: Warmwasser PUSH (`select.wh_warmwasser_warmwasser_push`, 60 min) |
| `automation.eos_warmepumpe_heizmodus_umsetzen` | setzt `sensor.eos_wp_sg_ready_soll` (2 min entprellt) über den gewählten Weg um |
| `automation.eos_warmepumpe_warmwasser_laden` | löst die WW-Ladung aus, wenn EOS HEAT_PUMP plant |

| `binary_sensor.wp_drosselung_ss14a` (KNX 9/1/0) | §14a-Sperre vom MVV-Binäreingang; geht als `wbb12-power-limited` an EOS |
| `binary_sensor.wp_sg1_h1_status` (KNX 9/1/11) | Status H1 (nur die Sperre schaltet H1) |
| `switch.wp_sg2_h2_anhebung` (KNX 9/1/20, Status 9/1/21) | H2 = SG Ready „Erhöhter Betrieb“ |
| `switch.wp_warmwasser_push_de2` (KNX 9/1/30, Status 9/1/31) | DE2 = Warmwasser PUSH |
| `script.eos_wp_sg_ready_kontakte` | Kontakt-Weg: SG 3/4 → H2 ein, sonst aus; Absenkung (SG 1) weiter über Modbus |
| `script.eos_wp_warmwasser_laden_kontakte` | Kontakt-Weg: DE2 für 60 min schließen |

Hinweise:

- **SG Ready per Modbus.** Die WBB 12 nimmt SG Ready nur über die Klemmen H1/H2 an; die Modbus-Werte
  `sensor.wh_eingange_ausgange_sg_ready_1/2` sind nur lesbar. Der Modbus-Weg bildet SG Ready deshalb über die
  Betriebsart des Heizkreises nach. Damit „Angehoben“ wirkt, muss die **Raumsolltemperatur Komfort** in der
  WP einmalig über **Normal** liegen. Heute stehen beide auf 23 °C (Absenk 22,5 °C).
- **Kontakte (geplant, SG Ready 1.1).** Die SG-Eingänge werden als „Leistungsbegr. SGR 1.1“
  konfiguriert (Eingänge → Eingang SGR1, wird auf SGR2 übertragen). Damit gilt laut Anleitung
  (Kap. 6.7.7.3):

  | Zustand | SGR1 (H1) | SGR2 (H2) |
  |---|---|---|
  | 1 Leistungsbegrenzung (Eingänge → Leistungsbegrenzung, 4,2 kW) | geschlossen | beliebig |
  | 2 Normalbetrieb | offen | offen |
  | 3 Erhöhter Betrieb | offen | geschlossen |

  Es gibt keinen Zustand „Sperre“ und keinen „Zwangsbetrieb“ (also auch keine Elektroheizung über SG 4).
  Das MVV-Steuerrelais (§14a) schaltet SGR1. HA schaltet nur SGR2 für „Angehoben“. Sind beide
  geschlossen, gewinnt die Leistungsbegrenzung – eine Verriegelung ist nicht nötig.
  „Reduziert“ läuft auch dann über Modbus (Betriebsart Absenk). **DE2** bleibt „Warmwasser PUSH“
  (K3 über Koppelrelais), **DE1** „Taupunktwächter“. Danach wird nur
  `input_select.eos_wp_ubertragungsweg` umgestellt; für SG 1 muss das Mapping dann auf 2 zeigen.
- **Heizstab.** Die Umsetzung folgt nach dem Einbau (Modbus über den Proxy).
- **Wirkung.** Die Automationen tun erst etwas, wenn EOS die Sensoren `sensor.eos_wbb12_heating_mode` und
  `sensor.eos_wbb12_dhw_mode` liefert (neues Image + Wärmepumpen-Konfiguration) **und** der Hauptschalter an ist.

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

# Wärmepumpe im RL29-Fork: Inbetriebnahme

Stand: Phase 3 (Optimierer) fertig. Die Home-Assistant-Seite unten ist ein **Entwurf**.
Sie wird erst nach Abstimmung angelegt. Nichts davon ist in HA aktiv.

## 1. Was EOS jetzt rechnet

Pro Wärmepumpe kommen zwei Gene pro Stunde in das Genom:

- ein **Heizmodus**: 0 REDUCED, 1 NORMAL, 2 RAISED, 3 PREHEAT;
- ein **Warmwassermodus**: 0 HOLD, 1 HEAT_PUMP, 2 ROD_60, 3 ROD_70.

Batterie, E-Auto, Geräte und Wärmepumpe werden gemeinsam optimiert. Der Strom von Wärmepumpe und Heizstab läuft
im Energiefluss vor dem Wechselrichter mit, so wie jeder andere Verbraucher.

In die Fitness gehen zusätzlich ein:

- Komfortunterschreitung (€/K·h);
- Warmwasser unter dem Minimum;
- ein verpasster Legionellenlauf;
- Moduswechsel (wenig Relaisschalten);
- der Wert der am Horizontende gespeicherten Wärme.

| Modus | SG Ready | Kontakte | Bedeutung |
|---|---|---|---|
| REDUCED | 1 (Sperre) | K1 an, K2 aus | Raum darf bis Komfortminimum auskühlen |
| NORMAL | 2 | K1 aus, K2 aus | Heizkurve wie heute |
| RAISED | 3 | K1 aus, K2 an | Anhebung (SG-Ready-WW-Anhebung = Aus!) |
| PREHEAT | 3 + Ventile offen | wie RAISED + KNX-Zwangsstellung | erst nach KNX-Umbau freigeben |
| HEAT_PUMP | – | DE2 „Warmwasser PUSH“ (oder Modbus-Select, s. u.) | WW auf WP-Sollwert |
| ROD_60 / ROD_70 | – | Heizstab-Freigabe + Zieltemperatur | Heizstab, WP nie über eigenen Sollwert |

SG-Ready-Zustand 4 wird nie ausgegeben. Legionellen laufen nur über den Heizstab. Fällig wird ein Lauf nach
`legionella_interval_days` (Standard 7) ab dem letzten Messwert ≥ 59,5 °C. Ohne Messhistorie wird kein Lauf verlangt.

## 2. EOS-Konfiguration (Vorschlag)

```json
{
  "devices": {
    "max_heat_pumps": 1,
    "heat_pumps": {
      "wbb12": {
        "dhw": {"volume_l": 190, "setpoint_c": 45, "rod_power_w": 3000, "legionella_interval_days": 7},
        "comfort": {"setpoint_c": 21.0, "max_c": 22.0},
        "optimization": {
          "heating_modes": [0, 1, 2],
          "dhw_modes": [0, 1, 2, 3],
          "load_forecast_includes_heat_pump": true
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
        "sensor.eos_wbb12_sg_ready_state",
        "sensor.eos_wbb12_dhw_push",
        "sensor.eos_wbb12_rod_target_c",
        "sensor.eos_wbb12_valves_forced",
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
- **`load_forecast_includes_heat_pump: true`** ist eine Übergangslösung. Die Lastprognose (LoadAkkudoktor mit 10.000 kWh/a) enthält heute die Wärmepumpe. EOS zieht deshalb den Status-quo-Verbrauch ab. Besser ist eine Prognose ohne WP. Dafür als Lastquelle `sensor.eos_grundlast_ohne_wp_gesamt` verwenden, die Jahresenergie entsprechend senken und danach den Schalter auf `false` stellen.
- **Gebäudeparameter** kommen aus `scripts/rl29/calibrate_heatpump.py`. Die Defaults sind Schätzwerte.
- Ein **Estrich-Messwert** existiert nicht. EOS schätzt ihn dann aus dem Gleichgewicht.

## 3. Netzentgelt Modul 3 (MVV)

```bash
python3 scripts/rl29/elecfee_modul3.py \
  --other-amt-kwh <13,796 ct minus Netz-Arbeitspreis, in EUR> \
  --regular-network-amt-kwh <Netz-Arbeitspreis Q2/Q3 laut Preisblatt 2, in EUR> \
  --apply http://eos.local:8503
```

Das Skript setzt diese Bänder (netto):

| Quartale | Zeitfenster | Arbeitspreis |
|---|---|---|
| Q1/Q4 | 02–06 Uhr (Niedrig) | 1,73 ct |
| Q1/Q4 | 00–02, 06–17, 20–24 Uhr (Standard) | 4,32 ct |
| Q1/Q4 | 17–20 Uhr (Hoch) | 5,96 ct |
| Q2/Q3 | ganztägig | regulärer Netz-Arbeitspreis |

Die Fenster nutzen das neue `months`-Feld. Modul 1 ist eine pauschale Kürzung und spielt für die Optimierung keine Rolle.

## 4. HA-Dispatcher (ENTWURF – nicht angelegt)

Voraussetzungen:

- 4-fach-Aktor verdrahtet: K1/K2 an H1/H2, K3 über Koppelrelais an DE2.
- Heizstab-Aktor vorhanden.

Die Entitätsnamen `switch.wp_sg_k1` usw. sind Platzhalter.

```yaml
alias: EOS – Wärmepumpe Modi umsetzen
mode: queued
triggers:
  - trigger: state
    entity_id: [sensor.eos_wbb12_sg_ready_state, sensor.eos_wbb12_dhw_push, sensor.eos_wbb12_rod_target_c]
  - trigger: time_pattern
    minutes: /5
conditions:
  - condition: state
    entity_id: input_boolean.eos_wp_steuerung_aktiv   # Hauptschalter, neu
    state: "on"
actions:
  - variables:
      sg: "{{ states('sensor.eos_wbb12_sg_ready_state') | int(2) }}"
      heizen: "{{ is_state('input_select.betriebsmodus_haus', 'Heizen') }}"
      sperre_ok: >-
        {{ states('sensor.raumtemperatur_kaltester_raum') | float(0) >= 19.5
           and states('sensor.wh_warmwasser_warmwassertemperatur') | float(0) >= 38 }}
  - choose:
      - conditions: "{{ heizen and sg == 1 and sperre_ok }}"
        sequence:
          - action: switch.turn_on
            target: {entity_id: switch.wp_sg_k1}
          - action: switch.turn_off
            target: {entity_id: switch.wp_sg_k2}
      - conditions: "{{ heizen and sg == 3 }}"
        sequence:
          - action: switch.turn_off
            target: {entity_id: switch.wp_sg_k1}
          - action: switch.turn_on
            target: {entity_id: switch.wp_sg_k2}
    default:
      - action: switch.turn_off
        target: {entity_id: [switch.wp_sg_k1, switch.wp_sg_k2]}
  # Warmwasser PUSH: Impuls auf DE2 (K3), nur bei Flanke 0 -> 1
  # Heizstab: Freigabe, wenn Ziel > Ist; aus bei Ziel 0 oder Ist >= Ziel
```

Sicherheitsautomationen (Entwurf):

1. **Sperre lösen.** SG 1 nie länger als 2 h am Stück. Sofort lösen, wenn der kälteste Raum unter 19,5 °C oder das WW unter 38 °C fällt.
2. **§14a-Dimmung.** Das MVV-Relais liegt parallel zu K1. Während der Dimmung bleibt der Heizstab aus. `wbb12-power-limited` als Messwert an EOS melden.
3. **Feuchte/Taupunkt.** Bei `binary_sensor.feuchte_warnung_wohnraume` keine Absenkung (SG 1 → 2).
4. **Ausfall.** Ist EOS länger als 30 min nicht aktuell (`sensor.eos_*` unverändert), alle Kontakte aus und damit Normalbetrieb.
5. **Betriebsmodus.** Bei Kühlen oder Warmwasser (Sommer) keine SG-Anhebung. Nur Warmwasser und Heizstab werden umgesetzt.

**Option ohne DE2-Verdrahtung:** `select.wh_warmwasser_warmwasser_push` existiert bereits über Modbus. Ein PUSH ist ein seltener Schreibzugriff (≤ 2×/Tag) und betrifft keinen Sollwert. Das ist eine vertretbare Ausnahme von „Modbus nur lesen“ – bitte entscheiden.

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

# RL29: Haushaltsgeräte auf Anforderung (Waschmaschine)

## EOS (Fork)

`devices.home_appliances.<id>.request_driven: true` – das Gerät wird nur geplant, solange die Steuerung einen
frischen Auftrag meldet (Messwerte, älter als `request_max_age_seconds` = kein Auftrag):

| Messwert | Bedeutung |
|---|---|
| `<id>-requested` | 1 = ein Lauf wartet (z. B. Fernstart erlaubt), 0 = nichts |
| `<id>-deadline` | Unix-Zeit, bis zu der der Lauf fertig sein muss (0 = keine Frist) |
| `<id>-duration` | Programmdauer [h] (nur bei `consumption_wh`/`duration_h`; mittlere Leistung bleibt) |

Lösungsschlüssel `<id>_next_start_ts`: Unix-Zeit des nächsten geplanten Starts ab dem jeweiligen Slot
(0 = keiner). Dazu wie bisher `<id>_energy_wh`, `<id>_run_op_mode` und die DDBC-Anweisung `<id>`.

Konfiguration Waschmaschine (Profil aus der KNX-Messung, 1-h-Slots: Heizphase zuerst):

```json
{
  "devices": {
    "max_home_appliances": 1,
    "home_appliances": {
      "waschmaschine": {
        "device_id": "waschmaschine",
        "request_driven": true,
        "learn_profiles": true,
        "num_cycles": 1,
        "load_profile_power_w": [1400, 250, 150, 100],
        "load_profile_interval_seconds": 3600,
        "deadline_policy": "BEST_EFFORT"
      }
    }
  },
  "adapter": {
    "homeassistant": {
      "device_measurement_entity_ids": {
        "waschmaschine-requested": "sensor.eos_waschmaschine_auftrag",
        "waschmaschine-deadline": "sensor.eos_waschmaschine_frist_zeitstempel",
        "waschmaschine-duration": "sensor.eos_waschmaschine_programmdauer",
        "waschmaschine-power": "sensor.strom_waschmaschine",
        "waschmaschine-program": "sensor.eos_waschmaschine_programm_code"
      },
      "device_measurement_history_keys": ["waschmaschine-power"],
      "solution_entity_ids": ["sensor.eos_waschmaschine_next_start_ts", "sensor.eos_waschmaschine_energy_wh"],
      "device_instruction_entity_ids": ["sensor.eos_waschmaschine"]
    }
  }
}
```

(Listen beim Einspielen mit den bestehenden Einträgen zusammenführen.)

## Home Assistant

| Objekt | Zweck |
|---|---|
| `input_boolean.eos_waschmaschine_steuerung_aktiv` | Hauptschalter; aus = kein Auftrag an EOS, Maschine bleibt unberührt |
| `sensor.eos_waschmaschine_auftrag` | 1 bei Fernstart erlaubt und Zustand `ready`/`delayedstart` |
| `sensor.eos_waschmaschine_frist_zeitstempel` | Frist als Unix-Zeit (aus `input_datetime.eos_waschmaschine_frist`) |
| `sensor.eos_waschmaschine_programmdauer` | Programmdauer [h] |
| `automation.eos_waschmaschine_auftrag_erkennen` | legt die Frist einmal fest: geplantes Ende ≤ 19:30 → fertig bis 19:30, sonst bis 6:00 |
| `automation.eos_waschmaschine_start_programmieren` | programmiert den EOS-Start als Startverzögerung an der Maschine |

Regeln beim Start:

1. EOS-Plan nach dem Auftrag und jünger als 45 min → `homeconnect_ws.start_program` mit `start_in`
   (unter 5 min: sofort). Die Maschine startet dann selbst, auch wenn HA ausfällt.
2. 30 min nach dem Auftrag noch kein Plan → Rückfall: `finish_in` = Frist (bei knapper Frist sofort), Push.
3. EOS verschiebt den Start um ≥ 60 min, alter und neuer Start > 30 min in der Zukunft → `set_start_in`,
   höchstens 2 Anpassungen.

Geplantes Ende = am Gerät eingestellte Endzeit (Zustand `delayedstart`), sonst jetzt + Programmdauer.

## Lastprofile je Programm lernen

`learn_profiles: true` lernt für jedes Programm ein eigenes Lastprofil (5-Minuten-Raster) aus zwei Messwerten:

| Messwert | Quelle (HA) |
|---|---|
| `<id>-power` | gemessene Leistung [W] – mit `adapter.homeassistant.device_measurement_history_keys` wird der volle HA-Verlauf importiert, nicht nur der Wert beim EOS-Lauf |
| `<id>-program` | Programm-Code (feste Zahl je Programm) |

Ablauf bei jedem EOS-Lauf:

1. Fertige Läufe erkennen: Leistung > `profile_power_threshold_w` (20 W), Pausen < 15 min gehören zum Lauf,
   ein Lauf gilt nach 15 min Ruhe als beendet. Das Programm ist der Code, der beim Start gesetzt war.
2. Lernen (Online-Lernen): die ersten Läufe eines Programms werden gleich gewichtet gemittelt, danach
   gleitender Mittelwert mit `profile_learning_rate` (0,3). Ausreißer wirken begrenzt, Änderungen werden
   übernommen. Die Länge wird genauso gemittelt.
3. Planen: Wartet ein Auftrag und gibt es ein gelerntes Profil für das gewählte Programm, plant EOS mit diesem
   Profil, sonst mit der Konfiguration.

Speicher: `<EOS-Datenordner>/rl29/appliance_profiles.json` (je Gerät und Programm: Profil, Anzahl Läufe, Energie).

Ausbaustufe: Sobald genug Läufe da sind, kann statt des Mittelwerts je Programm ein Regressionsmodell
(Programm + Temperatur + Schleuderdrehzahl + Beladung → Profil) gelernt werden.

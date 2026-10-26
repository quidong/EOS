# RL29: MCP-Zugang zu EOS

Ein eigener Container im EOS-Stack (`eos-mcp`, gleiches Image) bietet EOS als MCP-Server an
(Streamable HTTP, `http://eos.local:8505/mcp`). Er spricht nur mit der EOS-REST-API im
Stack-Netz (`http://eos:8503`) und liest die Modellberichte aus dem Datenvolume (nur lesend).

Code: `src/akkudoktoreos/rl29mcp/` (`security.py` Schutzmechanismen, `server.py` Tools),
Tests: `tests/rl29/test_mcp.py`, `tests/rl29/test_config_secrets.py`.

## Tools

| Tool | Art | Inhalt |
| --- | --- | --- |
| `eos_status` | lesen | Gesundheit, Algorithmus (inkl. A/B), letzter Plan mit Kosten und Baseline |
| `eos_plan` | lesen | Plan je Slot für die nächsten Stunden, Spalten filterbar |
| `eos_forecast` | lesen | Prognosereihe eines Schlüssels (Preis, PV, Last, Wetter) |
| `eos_kpis` | lesen | Prognose-/Plangüte, realisierte Netzkosten je Tag und Algorithmus |
| `eos_hours` | lesen | Ist vs. Prognose je Stunde |
| `eos_reports` | lesen | Berichte Grundlast-ML, Preis-ML, PV-Ensemble |
| `eos_config_get` | lesen | Konfiguration (ganz oder Teilbaum), Geheimnisse maskiert |
| `eos_config_set` | schreiben | einen Wert ändern (nur freigegebene Bereiche, Begründung Pflicht) |
| `eos_config_undo` | schreiben | letzte Änderung zurücknehmen |
| `eos_config_save` | schreiben | laufende Konfiguration in die Datei schreiben |

Schreib-Tools gibt es nur mit `EOS_MCP_ALLOW_WRITE=true`.

## Absicherung

- **Token-Pflicht:** Jede Anfrage braucht `Authorization: Bearer <EOS_MCP_TOKEN>` (mindestens
  32 Zeichen, zufällig; Vergleich in konstanter Zeit). Ohne gültiges Token startet der Server
  nicht. Offen ist nur `GET /health` (ohne Inhalt) für den Healthcheck.
- **Host-Prüfung:** `EOS_MCP_ALLOWED_HOSTS` (z. B. `eos.local:8505`) schützt gegen
  DNS-Rebinding aus dem Browser.
- **Nur lesen als Standard:** Schreiben muss ausdrücklich eingeschaltet werden.
- **Allow-List:** Geändert werden dürfen nur Pfade unter `EOS_MCP_WRITE_PATHS` (Standard:
  `ems`, `optimization`, `load/provider`, `load/loadml`, `pvforecast/ensemble`, `elecprice/ml`,
  `database/history/{enabled,actuals,accuracy,min_store_interval_minutes}`,
  `devices/heat_pumps/*/{comfort,optimization,dhw,away}`,
  `devices/batteries/*/{min,max}_soc_percentage`). Nie: `adapter`, `server`, `general`,
  `logging`, `cache`, `measurement` und alles, was ein Geheimnis ist.
- **Geheimnisse:** werden nie angezeigt und können über MCP nicht gesetzt werden.
- **Grenzen:** höchstens `EOS_MCP_MAX_WRITES_PER_MINUTE` (10) Änderungen pro Minute, Werte
  höchstens 64 KB, Pfade nur `a/b/c` aus Buchstaben, Ziffern, `_`, `-`.
- **Nachvollziehbarkeit:** Jede Änderung steht mit altem und neuem Wert und Begründung im
  Container-Log (`MCP audit: …`). Änderungen wirken sofort, dauerhaft erst nach
  `eos_config_save`; `eos_config_undo` nimmt die letzte zurück.
- **Container:** läuft als Benutzer `eos` (100:101), Datenvolume nur lesend, kein Zugriff auf
  die Datenbank oder Home Assistant.

### Geheimnisse in der EOS-REST-API

Beim Einrichten fiel auf: Die EOS-REST-API hat keine Anmeldung und gab die Konfiguration samt
Home-Assistant-Token und Datenbank-Passwort an jeden im Netz heraus
(`GET /v1/config/adapter/homeassistant/token`). Ab diesem Release maskiert EOS alle
Geheimnisse in Konfigurationsantworten (`***`, beim DSN nur das Passwort). Werte, die maskiert
zurückkommen (EOSdash-Export/-Import), lassen das gespeicherte Geheimnis unverändert.

Weiterhin gilt: Wer Port 8503 erreicht, kann die Konfiguration ändern. Port 8503/8504 sollte
nur aus dem Server-VLAN und von Home Assistant erreichbar sein (Firewall-Regel, setzt Bewohner A
selbst). Den HA-Token nach dem Update erneuern, da er bisher lesbar war.

## Stack (Portainer, VM "energy")

Zusätzlicher Dienst im `eos`-Stack:

```yaml
  eos-mcp:
    image: ghcr.io/quidong/eos:rl29-<sha>      # gleiches Image wie eos
    container_name: eos-mcp
    restart: unless-stopped
    user: "100:101"
    command: ["python", "-m", "akkudoktoreos.rl29mcp"]
    environment:
      EOS_MCP_TOKEN: ${EOS_MCP_TOKEN}
      EOS_MCP_EOS_URL: http://eos:8503
      EOS_MCP_PORT: "8505"
      EOS_MCP_ALLOWED_HOSTS: eos.local:8505
      EOS_MCP_ALLOW_WRITE: "true"
      EOS_MCP_REPORT_DIR: /data/rl29
    volumes:
      - eos_data:/data:ro
    ports:
      - "8505:8505"
    depends_on:
      - eos
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8505/health', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
    labels:
      - com.centurylinklabs.watchtower.enable=false
```

`EOS_MCP_TOKEN` als Stack-Umgebungsvariable in Portainer anlegen, erzeugen mit
`openssl rand -hex 32`.

## Client

Claude Code:

```bash
claude mcp add --transport http eos http://eos.local:8505/mcp \
  --header "Authorization: Bearer <EOS_MCP_TOKEN>"
```

Claude Desktop (`claude_desktop_config.json`, über `mcp-remote`):

```json
{
  "mcpServers": {
    "eos": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://eos.local:8505/mcp", "--allow-http",
               "--header", "Authorization: Bearer ${EOS_MCP_TOKEN}"],
      "env": {"EOS_MCP_TOKEN": "<token>"}
    }
  }
}
```

Test ohne Client:

```bash
curl -s http://eos.local:8505/health                       # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://eos.local:8505/mcp   # 401
```

### Claude Desktop als Erweiterung (ohne Dateien zu bearbeiten)

`scripts/rl29/mcpb/` enthält eine Claude-Desktop-Erweiterung (MCPB), die `mcp-remote` mitbringt
und Adresse und Token beim Installieren abfragt (Token wird als vertraulich gespeichert):

```bash
cd scripts/rl29/mcpb/server && npm ci --omit=dev && cd ..
npx -y @anthropic-ai/mcpb@2.1.2 pack . eos-rl29.mcpb
```

Installieren: Claude Desktop → Einstellungen → Erweiterungen → `eos-rl29.mcpb` hineinziehen (oder
doppelklicken), Token eintragen, aktivieren.

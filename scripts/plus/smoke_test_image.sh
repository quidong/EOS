#!/usr/bin/env bash
# Smoke test for an EOS container image (eos-plus fork).
#
# Usage: scripts/plus/smoke_test_image.sh <image-ref> [expected-version]
#
# SMOKE_CONFIG=<file>: load this configuration (e.g. the quick start example), check that
# it is applied and saved, and wait up to SMOKE_PLAN_TIMEOUT seconds (default 0 = do not
# wait) for the first optimization plan. A missing plan is reported as a warning only,
# because it depends on external forecast services.
#
# Starts the image with an empty data volume, waits for the server to become
# healthy and checks the endpoints that the HA adapter and the dashboard rely
# on. Exits non-zero on the first failure. Needs docker, curl and python3.

set -euo pipefail

IMAGE="${1:?usage: $0 <image-ref> [expected-version]}"
EXPECTED_VERSION="${2:-}"
PORT="${SMOKE_PORT:-18503}"
NAME="eos-smoke-$$"
BASE="http://127.0.0.1:${PORT}"
TIMEOUT_S="${SMOKE_TIMEOUT:-180}"

cleanup() {
    if [ "${KEEP_CONTAINER:-0}" != "1" ]; then
        docker rm -f "$NAME" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

fail() {
    echo "SMOKE FAIL: $*" >&2
    echo "---- container log (tail) ----" >&2
    docker logs --tail 80 "$NAME" >&2 || true
    exit 1
}

echo "Starting $IMAGE on port $PORT"
docker run -d --name "$NAME" \
    -p "${PORT}:8503" \
    -e TZ=Europe/Berlin \
    -e EOS_SERVER__HOST=0.0.0.0 \
    -e EOS_SERVER__PORT=8503 \
    -e EOS_SERVER__STARTUP_EOSDASH=false \
    "$IMAGE" >/dev/null

# 1. Health
deadline=$(( $(date +%s) + TIMEOUT_S ))
until curl -fsS "${BASE}/v1/health" -o /tmp/eos-smoke-health.json 2>/dev/null; do
    if [ "$(date +%s)" -gt "$deadline" ]; then
        fail "no healthy /v1/health within ${TIMEOUT_S}s"
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
        fail "container exited"
    fi
    sleep 3
done
echo "health: $(cat /tmp/eos-smoke-health.json)"

python3 - <<'PY' || fail "health payload check"
import json
health = json.load(open("/tmp/eos-smoke-health.json"))
assert health.get("status") == "alive", f"status={health.get('status')!r}"
assert health.get("version"), "no version in /v1/health"
print(f"health payload ok (EOS version {health['version']})")
PY

# The fork version lives in the image label (EOS keeps its own version scheme).
if [ -n "$EXPECTED_VERSION" ]; then
    label=$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$IMAGE")
    [ "$label" = "$EXPECTED_VERSION" ] || fail "image label version '$label' != '$EXPECTED_VERSION'"
    echo "image label version ok ($label)"
fi

# 2. Config is readable and contains the sections the fork depends on
curl -fsS "${BASE}/v1/config" -o /tmp/eos-smoke-config.json || fail "/v1/config"
python3 - <<'PY' || fail "config payload check"
import json
cfg = json.load(open("/tmp/eos-smoke-config.json"))
for key in ("general", "devices", "ems", "optimization", "prediction", "adapter", "load", "pvforecast", "elecprice", "weather"):
    assert key in cfg, f"missing config section {key!r}"
assert cfg["optimization"].get("algorithm") in ("GENETIC", "GENETIC0"), cfg["optimization"].get("algorithm")
print("config payload ok")
PY

# 3. Prediction providers are registered
curl -fsS "${BASE}/v1/prediction/providers" -o /tmp/eos-smoke-providers.json || fail "/v1/prediction/providers"
python3 - <<'PY' || fail "provider list check"
import json
providers = json.load(open("/tmp/eos-smoke-providers.json"))
names = set(providers if isinstance(providers, list) else providers.keys())
for p in ("PVForecastPVLib", "ElecPriceEnergyCharts", "LoadAkkudoktor", "LoadImport", "OpenMeteo"):
    assert p in names, f"provider {p} missing"
print(f"{len(names)} providers ok")
PY

# 4. Energy management plan endpoint answers (empty plan is fine in a fresh container)
code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/v1/energy-management/plan")
case "$code" in
    200|404) echo "plan endpoint ok ($code)" ;;
    *) fail "/v1/energy-management/plan returned HTTP $code" ;;
esac

# 5. Optional: the quick start configuration is accepted and produces a plan
if [ -n "${SMOKE_CONFIG:-}" ]; then
    code=$(curl -s -o /tmp/eos-smoke-put.json -w '%{http_code}' -X PUT \
        -H 'Content-Type: application/json' --data-binary "@${SMOKE_CONFIG}" "${BASE}/v1/config")
    [ "$code" = "200" ] || fail "PUT /v1/config with ${SMOKE_CONFIG} returned HTTP $code: $(head -c 600 /tmp/eos-smoke-put.json)"
    curl -fsS -X PUT "${BASE}/v1/config/file" -o /dev/null || fail "PUT /v1/config/file"
    SMOKE_CONFIG="$SMOKE_CONFIG" python3 - /tmp/eos-smoke-put.json <<'PY' || fail "configuration not applied"
import json, os, sys
wanted = json.load(open(os.environ["SMOKE_CONFIG"]))
got = json.load(open(sys.argv[1]))

def check(want, have, path):
    if isinstance(want, dict):
        for key, value in want.items():
            assert isinstance(have, dict) and key in have, f"{path}/{key} missing"
            check(value, have[key], f"{path}/{key}")
    elif isinstance(want, (int, float)) and not isinstance(want, bool):
        assert isinstance(have, (int, float)) and abs(have - want) < 1e-9, f"{path}: {have!r} != {want!r}"
    elif not isinstance(want, list):
        assert have == want, f"{path}: {have!r} != {want!r}"

for section in ("general", "pvforecast", "elecprice", "optimization", "ems", "devices"):
    check(wanted.get(section, {}), got.get(section, {}), section)
print("configuration applied and saved")
PY
    plan_timeout="${SMOKE_PLAN_TIMEOUT:-0}"
    if [ "$plan_timeout" -gt 0 ]; then
        deadline=$(( $(date +%s) + plan_timeout ))
        until [ "$(curl -s -o /tmp/eos-smoke-plan.json -w '%{http_code}' "${BASE}/v1/energy-management/plan")" = "200" ]; do
            if [ "$(date +%s)" -gt "$deadline" ]; then
                echo "::warning title=smoke::no optimization plan within ${plan_timeout}s (forecast services?)"
                docker logs --tail 40 "$NAME" >&2 || true
                break
            fi
            sleep 10
        done
        [ -s /tmp/eos-smoke-plan.json ] && echo "plan: $(head -c 300 /tmp/eos-smoke-plan.json)"
    fi
fi

# 6. Docker healthcheck reports healthy
deadline=$(( $(date +%s) + 120 ))
while :; do
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$NAME")
    [ "$status" = "healthy" ] && break
    [ "$status" = "none" ] && { echo "image has no HEALTHCHECK"; break; }
    [ "$(date +%s)" -gt "$deadline" ] && fail "docker health status '$status'"
    sleep 5
done
echo "docker health: ${status}"

echo "SMOKE OK: $IMAGE"

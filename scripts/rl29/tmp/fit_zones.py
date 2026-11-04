"""Grid fit of the two zone models against measured zone temperatures (RL29, 03./04.11.)."""

import itertools
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from akkudoktoreos.devices.thermal.building import BuildingParameters
from akkudoktoreos.devices.thermal.cop import WBB12_RATING_POINTS, CopModel
from akkudoktoreos.devices.thermal.dhw import DhwTankParameters
from akkudoktoreos.devices.thermal.heatpump import HeatingMode as M, HeatPumpSystem, SlotInputs
from akkudoktoreos.devices.thermal.zones import MultiZoneHeatPump, ZoneCoupling, ZoneParameters

TZ = timezone(timedelta(hours=1))
START = datetime(2026, 11, 3, 14, 15, tzinfo=TZ)
END = datetime(2026, 11, 4, 6, 15, tzinfo=TZ)
STEP = timedelta(minutes=15)


def series(key):
    q = urllib.parse.urlencode(
        {"key": key, "start_datetime": (START - timedelta(hours=1)).isoformat(),
         "end_datetime": END.isoformat()}
    )
    d = json.load(urllib.request.urlopen("http://localhost:8503/v1/measurement/series?" + q))
    d = d.get("data", d)
    return sorted((datetime.fromisoformat(t), v) for t, v in d.items() if v is not None)


def at(points, t):
    best = None
    for pt, v in points:
        if pt <= t:
            best = v
        else:
            break
    return best


og, eg, out = (series(k) for k in ("wbb12-og-room-temperature", "wbb12-eg-room-temperature",
                                     "wbb12-outdoor-temperature"))
slots = []
t = START
while t < END:
    slots.append(t)
    t += STEP


def modes(t):
    """Zone modes (og, eg) as executed by HA, reconstructed from the SG/forcing log."""
    h = lambda s: datetime.fromisoformat(s).replace(tzinfo=TZ)  # noqa: E731
    if t < h("2026-11-03T19:30"):
        return M.NORMAL, M.REDUCED  # EG setpoint 19 C (old zone logic), SG 2
    if t < h("2026-11-03T20:00"):
        return M.NORMAL, M.NORMAL
    if t < h("2026-11-03T21:15"):
        return M.REDUCED, M.REDUCED  # SG 1
    if t < h("2026-11-03T21:45"):
        return M.NORMAL, M.FORCED  # SG 4, EG forced
    if t < h("2026-11-03T22:15"):
        return M.RAISED, M.RAISED  # SG 4 without forcing, setpoints fixed
    if t < h("2026-11-04T03:15"):
        return M.REDUCED, M.REDUCED  # SG 1
    return M.NORMAL, M.NORMAL  # SG 2


TANK = DhwTankParameters(volume_l=190, loss_w_per_k=1.5, ambient_c=18)
HOUSE = BuildingParameters(3500, 10000, 180, 1500, 1000)


def simulate(og_b, eg_b, coupling=100.0):
    from akkudoktoreos.devices.thermal.heatpump import ComfortParameters

    system = HeatPumpSystem(HOUSE, TANK, CopModel(WBB12_RATING_POINTS), slot_hours=0.25,
                            comfort=ComfortParameters(fixed_room_setpoints=False))
    hp = MultiZoneHeatPump(system, [ZoneParameters("og", og_b, weight=80),
                                    ZoneParameters("eg", eg_b, weight=160)],
                           [ZoneCoupling(0, 1, coupling)])
    state = hp.initial_state([at(og, slots[0]), at(eg, slots[0])], 45.0, at(out, slots[0]))
    err_og = err_eg = 0.0
    sim = []
    for t in slots:
        inp = SlotInputs(outdoor_c=at(out, t), comfort_min_c=19.0, setpoint_c=21.0,
                         comfort_max_c=23.0)
        res = hp.step(state, inp, list(modes(t)))
        state = res.state
        t1 = t + STEP
        m_og, m_eg = at(og, t1), at(eg, t1)
        err_og += (state.rooms[0] - m_og) ** 2
        err_eg += (state.rooms[1] - m_eg) ** 2
        sim.append((t1.strftime("%H:%M"), round(state.rooms[0], 2), m_og,
                    round(state.rooms[1], 2), m_eg))
    n = len(slots)
    return (err_og / n) ** 0.5, (err_eg / n) ** 0.5, sim


cur_og = BuildingParameters(2200, 3300, 60, 500, 330)
cur_eg = BuildingParameters(1300, 6700, 120, 1000, 670)
r_og, r_eg, sim = simulate(cur_og, cur_eg)
print(f"current: rmse og {r_og:.2f} K, eg {r_eg:.2f} K")
for row in sim[::8]:
    print("  ", row)

best = []
for loss, emit, sr in itertools.product((130, 160, 200, 250), (60, 80, 100, 130, 150), (100, 150, 250)):
    b = BuildingParameters(3300, 3300, loss, sr, emit)
    r1, r2, _ = simulate(b, cur_eg)
    best.append((r1, r2, loss, emit, sr))
best.sort()
print("best og (rmse og, rmse eg, loss, emitter, slab_room):")
for row in best[:6]:
    print("  ", tuple(round(x, 3) if isinstance(x, float) else x for x in row))
og_b = BuildingParameters(3300, 3300, best[0][2], best[0][4], best[0][3])
best_eg = []
for loss, emit, sr in itertools.product((60, 80, 100, 120), (500, 670, 900, 1200), (1000, 1500)):
    b = BuildingParameters(1300, 6700, loss, sr, emit)
    r1, r2, _ = simulate(og_b, b)
    best_eg.append((r1 + r2, r1, r2, loss, emit, sr))
best_eg.sort()
print("best eg with best og (sum, rmse og, rmse eg, loss, emitter, slab_room):")
for row in best_eg[:6]:
    print("  ", tuple(round(x, 3) if isinstance(x, float) else x for x in row))
eg_b = BuildingParameters(1300, 6700, best_eg[0][3], best_eg[0][5], best_eg[0][4])
for c in (0.0, 100.0, 200.0):
    r1, r2, sim = simulate(og_b, eg_b, coupling=c)
    print(f"coupling {c}: og {r1:.2f} eg {r2:.2f}")
r1, r2, sim = simulate(og_b, eg_b)
print("best trace (time, sim og, meas og, sim eg, meas eg):")
for row in sim[::8]:
    print("  ", row)

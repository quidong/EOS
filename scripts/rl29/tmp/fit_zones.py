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
for loss, emit, sr, slab in itertools.product(
    (60, 75, 90, 110, 130), (150, 200, 250, 330), (250, 350, 500), (2200, 3300)
):
    b = BuildingParameters(slab, 3300, loss, sr, emit)
    r1, r2, _ = simulate(b, cur_eg)
    best.append((r1, r2, loss, emit, sr, slab))
best.sort()
print("best og (rmse og, rmse eg, loss, emitter, slab_room, slab_cap):")
for row in best[:8]:
    print("  ", tuple(round(x, 3) if isinstance(x, float) else x for x in row))
b = BuildingParameters(best[0][5], 3300, best[0][2], best[0][4], best[0][3])
_, _, sim = simulate(b, cur_eg)
print("best og trace (time, sim og, meas og, sim eg, meas eg):")
for row in sim[::8]:
    print("  ", row)

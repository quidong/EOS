"""One airing recommendation per day that stays put between runs (RL29 fork).

Every run picks the airing slot from the slots ahead. Without a memory the
afternoon runs marked the current slot again and again (the warmest remaining
slot), and the controller sent one notification per run. The store keeps the
announced slot of the day, and a day on which at least 70 % of the windows were
already open gets no recommendation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from collections.abc import AsyncGenerator

from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.heatpumpventilation import (
    VentilationStore,
    aired_in_window,
    fixed_days,
    record_ventilation,
    store_path,
)
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
)

TZ = "Europe/Berlin"
BERLIN = ZoneInfo(TZ)
SUNDAY = pendulum.datetime(2026, 11, 1, tz=TZ)
SLOT_H = 0.25


def settings(**ventilation) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings(
        device_id="wbb12",
        ventilation={"enabled": True, "recommend": True, **ventilation},
    )


def outdoor_at(start: datetime) -> float:
    """Warmest at 13:00, cooling in the afternoon (as on 1 Nov 2026)."""
    hour = start.hour + start.minute / 60.0
    return 8.0 - abs(hour - 13.0)


def run(now: datetime, fixed=None, hours: int = 12, device=None, temps=outdoor_at):
    first = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    starts = [first + timedelta(minutes=15 * i) for i in range(int(hours / SLOT_H))]
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=device or settings(),
        initial_room_c=20.0,
        initial_dhw_c=45.0,
        ventilation_fixed=fixed,
    )
    n = len(starts)
    plan = build_run_plan(
        params, starts, SLOT_H, [temps(s) for s in starts], [0.0] * n, [0.0003] * n
    )
    return starts, plan


def simulate_day(tmp_path: Path, start_hour=10, end_hour=17, aired_from=None, share=1.0):
    """Runs every 15 min (1 min after the slot start) with the store in between.

    Returns the times at which the current-slot flag (what the controller reads)
    rises from 0 to 1 - one notification each.
    """
    path = store_path(tmp_path)
    previous = None  # (starts, flags) of the latest plan
    shown = 0.0
    rises = []
    t = SUNDAY.replace(hour=start_hour, minute=1)

    def flag(plan, at: datetime) -> float:
        starts, flags = plan
        for s, f in zip(starts, flags):
            if s <= at < s + timedelta(minutes=15):
                return f
        return 0.0

    while t.hour < end_hour:
        slot = t - timedelta(minutes=1)
        # 30 s after the slot start the controller still reads the previous plan
        if previous is not None:
            value = flag(previous, slot + timedelta(seconds=30))
            if value and not shown:
                rises.append(slot)
            shown = value
        aired = aired_from is not None and t >= aired_from and share >= 0.7
        fixed = fixed_days(VentilationStore(path), "wbb12", t, aired)
        starts, plan = run(t, fixed)
        flags = [1.0 if v else 0.0 for v in plan.ventilate_slots]
        record_ventilation("wbb12", tmp_path, t, starts, flags)
        previous = (starts, flags)
        value = flag(previous, t)
        if value and not shown:
            rises.append(slot)
        shown = value
        t = t + timedelta(minutes=15)
    return rises


def test_without_memory_every_afternoon_run_marks_the_current_slot():
    flagged_now = []
    t = SUNDAY.replace(hour=13, minute=1)
    while t.hour < 15:
        starts, plan = run(t)
        if plan.ventilate_slots[0]:
            flagged_now.append(starts[0])
        t += timedelta(minutes=15)
    assert len(flagged_now) >= 6  # the bug: a "now" recommendation in every run


def test_one_notification_per_day_with_the_memory(tmp_path: Path):
    rises = simulate_day(tmp_path)
    assert rises == [SUNDAY.replace(hour=13, minute=0)]
    stored = VentilationStore(store_path(tmp_path)).slot("wbb12", "2026-11-01")
    assert stored == SUNDAY.replace(hour=13, minute=0)


def test_a_pending_slot_may_move_until_it_starts(tmp_path: Path):
    path = store_path(tmp_path)
    store = VentilationStore(path)
    morning = SUNDAY.replace(hour=9, minute=1)
    starts = [SUNDAY.replace(hour=9) + timedelta(minutes=15 * i) for i in range(32)]
    flags = [0.0] * 32
    flags[16] = 1.0  # 13:00
    store.record("wbb12", morning, starts, flags)
    assert fixed_days(store, "wbb12", morning, False) == {}  # not started: still open
    flags = [0.0] * 32
    flags[20] = 1.0  # a newer forecast prefers 14:00
    store.record("wbb12", morning, starts, flags)
    assert store.slot("wbb12", "2026-11-01") == SUNDAY.replace(hour=14)
    later = SUNDAY.replace(hour=14, minute=1)
    assert fixed_days(store, "wbb12", later, False) == {
        "2026-11-01": SUNDAY.replace(hour=14).isoformat()
    }
    # once started it is final, whatever later runs mark
    after = [SUNDAY.replace(hour=14, minute=15) + timedelta(minutes=15 * i) for i in range(8)]
    store.record("wbb12", SUNDAY.replace(hour=14, minute=16), after, [1.0] + [0.0] * 7)
    assert store.slot("wbb12", "2026-11-01") == SUNDAY.replace(hour=14)
    store.record("wbb12", SUNDAY.replace(hour=14, minute=31), after[1:], [0.0] * 7)
    assert store.slot("wbb12", "2026-11-01") == SUNDAY.replace(hour=14)


def test_a_day_without_a_marked_slot_drops_the_pending_one(tmp_path: Path):
    store = VentilationStore(store_path(tmp_path))
    t = SUNDAY.replace(hour=11, minute=1)
    starts = [SUNDAY.replace(hour=11) + timedelta(minutes=15 * i) for i in range(16)]
    store.record("wbb12", t, starts, [0.0] * 8 + [1.0] + [0.0] * 7)
    store.record("wbb12", t + timedelta(minutes=15), starts[1:], [0.0] * 15)
    assert store.slot("wbb12", "2026-11-01") is None


def test_announced_slot_is_kept_and_nothing_new_after_it(tmp_path: Path):
    fixed = {"2026-11-01": SUNDAY.replace(hour=13).isoformat()}
    starts, plan = run(SUNDAY.replace(hour=13, minute=1), fixed)
    assert [starts[i] for i, v in enumerate(plan.ventilate_slots) if v] == [SUNDAY.replace(hour=13)]
    assert plan.inputs[0].ventilation_w_per_k > 0  # the airing is still planned
    starts, plan = run(SUNDAY.replace(hour=13, minute=16), fixed)
    assert not any(plan.ventilate_slots[: int(10 / SLOT_H)])  # rest of the day: none
    # the next day is chosen freely
    starts, plan = run(SUNDAY.replace(hour=13, minute=16), fixed, hours=24)
    marked = [starts[i] for i, v in enumerate(plan.ventilate_slots) if v]
    assert [s.date().isoformat() for s in marked] == ["2026-11-02"]


def test_aired_with_70_percent_skips_the_day(tmp_path: Path):
    rises = simulate_day(tmp_path, aired_from=SUNDAY.replace(hour=11, minute=30), share=0.8)
    assert rises == []
    rises = simulate_day(
        tmp_path / "less", aired_from=SUNDAY.replace(hour=11, minute=30), share=0.6
    )
    assert rises == [SUNDAY.replace(hour=13, minute=0)]


def test_airing_after_the_announcement_keeps_it(tmp_path: Path):
    store = VentilationStore(store_path(tmp_path))
    store.days("wbb12")["2026-11-01"] = SUNDAY.replace(hour=13).isoformat()
    fixed = fixed_days(store, "wbb12", SUNDAY.replace(hour=13, minute=5), True)
    assert fixed == {"2026-11-01": SUNDAY.replace(hour=13).isoformat()}


def test_aired_in_window_needs_the_share_inside_the_window():
    day = SUNDAY
    now = SUNDAY.replace(hour=14)
    at = lambda h, m=0: SUNDAY.replace(hour=h, minute=m)  # noqa: E731
    assert not aired_in_window([(at(9), 1.0), (at(10), 0.0)], day, 11, 15, now, 0.7)
    assert aired_in_window([(at(12, 10), 0.75), (at(12, 20), 0.0)], day, 11, 15, now, 0.7)
    assert not aired_in_window([(at(12, 10), 0.69)], day, 11, 15, now, 0.7)
    # open since before the window counts
    assert aired_in_window([(at(10, 50), 0.8)], day, 11, 15, now, 0.7)
    # later than now or after the window does not
    assert not aired_in_window([(at(15, 30), 1.0)], day, 11, 15, SUNDAY.replace(hour=16), 0.7)
    assert not aired_in_window([(at(12), 1.0)], day, 11, 15, SUNDAY.replace(hour=10), 0.7)


def test_store_keeps_a_week_and_survives_a_broken_file(tmp_path: Path):
    path = store_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    store = VentilationStore(path)
    store.days("wbb12")["2026-10-01"] = "2026-10-01T13:00:00+02:00"
    t = SUNDAY.replace(hour=12)
    store.record("wbb12", t, [t], [1.0])
    store.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data["devices"]["wbb12"]) == ["2026-11-01"]


def test_skip_share_setting():
    assert settings().ventilation.recommend_skip_aired_share == 0.7
    assert settings(recommend_skip_aired_share=None).ventilation.recommend_skip_aired_share is None
    with pytest.raises(ValueError):
        settings(recommend_skip_aired_share=1.5)


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_request_passes_the_decided_days(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest

    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 8,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                },
            },
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "max_home_appliances": 0,
                "home_appliances": {},
                "max_heat_pumps": 1,
                "heat_pumps": {"wbb12": {"ventilation": {"enabled": True, "recommend": True}}},
            },
        }
    )
    now = pendulum.datetime(2026, 11, 20, 12, 31, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    await isolated_measurement.update_value(now, "wbb12-room-temperature", 20.5)
    await isolated_measurement.update_value(now, "wbb12-dhw-temperature", 45.0)

    def request() -> ConfigOptimizationRequest:
        return ConfigOptimizationRequest.model_validate(
            {
                "forecasts": {
                    "pv_forecast_wh": [0.0] * 48,
                    "total_load": [300.0] * 48,
                    "electricity_price_per_wh": [0.0003] * 48,
                    "feed_in_tariff_per_wh": [0.00007] * 48,
                    "temperature_forecast": [4.0] * 48,
                    "ghi_forecast": [0.0] * 48,
                }
            }
        )

    state = (await request().resolve()).heat_pumps[0]
    assert state.ventilation_fixed == {}
    # an announced slot of today comes back as decided
    folder = Path(config_eos.general.data_folder_path)
    store = VentilationStore(store_path(folder))
    store.days("wbb12")["2026-11-20"] = "2026-11-20T12:00:00+01:00"
    store.save()
    state = (await request().resolve()).heat_pumps[0]
    assert state.ventilation_fixed == {"2026-11-20": "2026-11-20T12:00:00+01:00"}
    # aired widely before: today has no recommendation (unless already announced)
    store.days("wbb12").clear()
    store.save()
    await isolated_measurement.update_value(now.subtract(minutes=50), "wbb12-window-open", 0.8)
    await isolated_measurement.update_value(now.subtract(minutes=40), "wbb12-window-open", 0.0)
    state = (await request().resolve()).heat_pumps[0]
    assert state.ventilation_fixed == {"2026-11-20": None}


@pytest.mark.asyncio
async def test_optimization_run_records_the_announced_slot(config_eos: ConfigEOS):
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
    from akkudoktoreos.utils.datetimeutil import to_datetime

    hours = 24
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {
                    "horizon_hours": hours,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "FIXED",
                }
            },
        }
    )
    start = to_datetime("2026-11-06T00:00:00", in_timezone=TZ)
    get_ems(init=True).set_start_datetime(start)
    outdoor = [2.0] * hours
    outdoor[13] = 7.0
    params = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * hours,
                "gesamtlast": [300.0] * hours,
                "strompreis_euro_pro_wh": [0.0003] * hours,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * hours,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": outdoor,
            "pv_battery": None,
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "wbb12",
                    "settings": settings(),
                    "initial_room_c": 20.8,
                    "initial_dhw_c": 45.0,
                }
            ],
        }
    )
    solution = GeneticOptimization(fixed_seed=3).optimize_ems(params, ngen=3, individuals=10)
    assert solution.heat_pumps["wbb12"]["ventilate"][13] == 1.0
    store = VentilationStore(store_path(Path(config_eos.general.data_folder_path)))
    # recorded as pending (13:00 has not started at midnight)
    assert store.slot("wbb12", "2026-11-06") == start.add(hours=13)

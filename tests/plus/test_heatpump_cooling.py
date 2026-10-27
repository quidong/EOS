"""Floor cooling with PV surplus only (eos-plus fork).

In the cooling season the optimizer chooses per hour between NORMAL (no
active cooling) and COOL. Cooling is limited by the dew point and, with
``cooling.pv_only``, by the forecast PV surplus of the hour.
"""

from __future__ import annotations

from typing import Any

import pendulum
import pytest

from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import (
    DhwMode,
    HeatingMode,
    SlotInputs,
    ThermalState,
)
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import (
    GeneticOptimizationParameters,
)
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    build_run_plan,
    plan_result,
)
from akkudoktoreos.utils.datetimeutil import to_datetime

T0 = pendulum.datetime(2026, 7, 15, 8, 0, tz="Europe/Berlin")
COOLING = {"enabled": True, "setpoint_c": 24.0, "max_c": 26.0, "min_pv_surplus_w": 500.0}


def device(**cooling: Any) -> HeatPumpCommonSettings:
    return HeatPumpCommonSettings(device_id="wbb12", cooling={**COOLING, **cooling})


def summer_system(**cooling: Any):
    return device(**cooling).to_system(1.0)


def hot_inputs(**kwargs: Any) -> SlotInputs:
    data = {
        "outdoor_c": 32.0,
        "comfort_min_c": 20.0,
        "setpoint_c": 24.0,
        "comfort_max_c": 26.0,
        "ghi_w_per_m2": 700.0,
        "cooling_season": True,
    }
    data.update(kwargs)
    return SlotInputs(**data)


WARM = ThermalState(slab_c=26.0, room_c=26.0, dhw_c=48.0)


# ----------------------------------------------------------------------
# Thermal model
# ----------------------------------------------------------------------


def test_cool_extracts_heat_and_uses_eer():
    system = summer_system()
    result = system.step(WARM.copy(), hot_inputs(), HeatingMode.COOL)
    assert result.heat_space_wh < 0
    cooling_wh = -result.heat_space_wh
    standby = system.hp.standby_w
    eer = system.cooling_eer(32.0, result.flow_c)
    assert eer > 4.5  # rated at 35 degC, the afternoon is milder
    assert result.hp_electric_wh == pytest.approx(cooling_wh / eer + standby, rel=1e-6)
    assert result.cop_space == pytest.approx(eer)
    passive = system.step(WARM.copy(), hot_inputs(), HeatingMode.NORMAL)
    assert result.state.room_c < passive.state.room_c


def test_eer_follows_outdoor_and_flow_temperature():
    system = summer_system()
    assert system.cooling_eer(35.0, 18.0) == pytest.approx(4.5)  # rated point
    hot = system.cooling_eer(38.0, 18.0)
    mild = system.cooling_eer(24.0, 18.0)
    assert hot < 4.5 < mild
    # a dew point limited (warmer) flow cools more efficiently
    assert system.cooling_eer(32.0, 23.0) > system.cooling_eer(32.0, 18.0)
    # the cooling in a hot slot costs more electricity than in a mild one
    hot_slot = system.step(WARM.copy(), hot_inputs(outdoor_c=38.0), HeatingMode.COOL)
    mild_slot = system.step(WARM.copy(), hot_inputs(outdoor_c=24.0), HeatingMode.COOL)
    assert hot_slot.heat_space_wh == pytest.approx(mild_slot.heat_space_wh)
    assert hot_slot.hp_electric_wh > mild_slot.hp_electric_wh
    # the old behaviour on request
    constant = summer_system(eer_constant=True)
    assert constant.cooling_eer(38.0, 18.0) == 4.5


def test_no_space_heating_in_the_cooling_season():
    system = summer_system()
    cold = ThermalState(slab_c=19.0, room_c=19.0, dhw_c=48.0)
    result = system.step(cold, hot_inputs(outdoor_c=12.0, ghi_w_per_m2=0.0), HeatingMode.NORMAL)
    assert result.heat_space_wh == 0.0


def test_zero_budget_means_no_cooling():
    system = summer_system()
    result = system.step(WARM.copy(), hot_inputs(cooling_budget_w=0.0), HeatingMode.COOL)
    assert result.heat_space_wh == 0.0


def test_budget_caps_the_electric_power():
    system = summer_system()
    result = system.step(WARM.copy(), hot_inputs(cooling_budget_w=300.0), HeatingMode.COOL)
    assert -result.heat_space_wh == pytest.approx(300.0 * system.cooling_eer(32.0, result.flow_c))
    assert result.hp_electric_wh == pytest.approx(300.0 + system.hp.standby_w)


def test_dew_point_limits_the_capacity():
    system = summer_system()
    free = system.step(WARM.copy(), hot_inputs(), HeatingMode.COOL)
    humid = system.step(WARM.copy(), hot_inputs(dewpoint_c=21.0), HeatingMode.COOL)
    assert -humid.heat_space_wh < -free.heat_space_wh
    assert humid.flow_c >= 21.0 + 2.0
    # slab already at the lowest admissible flow: nothing left to extract
    saturated = system.step(
        ThermalState(slab_c=24.5, room_c=26.0, dhw_c=48.0),
        hot_inputs(dewpoint_c=21.0),
        HeatingMode.COOL,
    )
    assert saturated.heat_space_wh == 0.0


def test_room_below_the_target_is_not_cooled():
    system = summer_system()
    cool_room = ThermalState(slab_c=23.0, room_c=23.0, dhw_c=48.0)
    result = system.step(cool_room, hot_inputs(), HeatingMode.COOL)
    assert result.heat_space_wh == 0.0


# ----------------------------------------------------------------------
# Run plan
# ----------------------------------------------------------------------


def make_plan(cooling_season: bool = True, surplus: list[float] | None = None, n: int = 12):
    params = GeneticHeatPumpParameters(
        device_id="wbb12",
        settings=device(),
        initial_room_c=25.5,
        initial_dhw_c=48.0,
        cooling_season=cooling_season,
        dewpoint_c=16.0,
    )
    starts = [T0.add(hours=i) for i in range(n)]
    return build_run_plan(
        params,
        starts,
        1.0,
        [31.0] * n,
        [700.0] * n,
        [0.0003] * n,
        surplus if surplus is not None else [3000.0] * 6 + [0.0] * (n - 6),
    )


def test_cooling_season_offers_normal_and_cool():
    plan = make_plan()
    assert plan.heating_modes == (int(HeatingMode.NORMAL), int(HeatingMode.COOL))
    assert all(i.cooling_season for i in plan.inputs)
    assert plan.inputs[0].cooling_budget_w == 3000.0
    assert plan.inputs[8].cooling_budget_w == 0.0  # no surplus: no cooling
    assert plan.inputs[0].comfort_max_c == 26.0


def test_heating_season_is_unchanged():
    plan = make_plan(cooling_season=False)
    assert HeatingMode.COOL not in plan.heating_modes
    assert not any(i.cooling_season for i in plan.inputs)


def test_cooling_only_in_surplus_hours():
    plan = make_plan()
    n = plan.n_periods
    genes = plan.genes_for_modes([HeatingMode.COOL] * n, [DhwMode.HOLD] * n)
    result = plan_result(plan, genes)
    heat = plan.evaluate(genes, detail=True).detail["heat_space_wh"]
    assert all(h < 0 for h in heat[:3])
    assert all(h == 0.0 for h in heat[6:])
    assert result["heating_mode"][0] == "COOL"


def test_small_surplus_below_the_minimum_is_not_used():
    plan = make_plan(surplus=[400.0] * 12)
    assert all(i.cooling_budget_w == 0.0 for i in plan.inputs)


def test_cooling_lowers_overheating():
    plan = make_plan(surplus=[4000.0] * 12)
    n = plan.n_periods
    cool = plan.evaluate(plan.genes_for_modes([HeatingMode.COOL] * n, [DhwMode.HOLD] * n))
    idle = plan.evaluate(plan.default_genes())
    assert cool.overheat_kh < idle.overheat_kh


# ----------------------------------------------------------------------
# Optimizer and configuration
# ----------------------------------------------------------------------


def test_optimizer_cools_with_pv_surplus(config_eos):
    config_eos.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 12,
                    "tail_horizon_hours": 0,
                    "interval_sec": 3600,
                    "terminal_value_mode": "FIXED",
                },
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-07-15T00:00:00"))
    slots = 12
    params = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [5000.0] * 7 + [0.0] * 5,
                "gesamtlast": [500.0] * slots,
                "strompreis_euro_pro_wh": [0.0003] * slots,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * slots,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": [32.0] * slots,
            "pv_battery": None,
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "wbb12",
                    "settings": device(),
                    "initial_room_c": 26.5,
                    "initial_dhw_c": 48.0,
                    "cooling_season": True,
                    "dewpoint_c": 15.0,
                }
            ],
        }
    )
    solution = GeneticOptimization(fixed_seed=4).optimize_ems(params, ngen=25, individuals=40)
    schedule = solution.heat_pumps["wbb12"]
    modes = schedule["heating_mode"]
    assert "COOL" in modes[:7]
    assert all(m == "NORMAL" for m in modes[7:])
    assert min(schedule["room_c"][:7]) < 26.5
    # no cooling heat extracted without PV surplus (hot water may still reheat)
    assert all(h >= 0.0 for h in schedule["heat_space_wh"][7:])


@pytest.mark.asyncio
async def test_config_request_reads_season_and_dewpoint(config_eos):
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    config_eos.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {"horizon_hours": 8, "interval_sec": 3600, "tail_horizon_hours": 0},
            },
            "devices": {
                "max_batteries": 0,
                "batteries": {},
                "max_electric_vehicles": 0,
                "electric_vehicles": {},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000}},
                "heat_pumps": {"wbb12": {"cooling": COOLING}},
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-07-15T10:00:00"))
    now = get_ems().start_datetime
    await measurement.update_value(now.subtract(minutes=3), "wbb12-cooling-season", 1.0)
    await measurement.update_value(now.subtract(minutes=3), "wbb12-dewpoint", 17.5)
    try:
        prepared = await ConfigOptimizationRequest.model_validate(
            {
                "forecasts": {
                    "pv_forecast_wh": [0.0] * 48,
                    "total_load": [300.0] * 48,
                    "electricity_price_per_wh": [0.0003] * 48,
                    "feed_in_tariff_per_wh": [0.00007] * 48,
                    "temperature_forecast": [30.0] * 48,
                }
            }
        ).resolve()
    finally:
        await measurement.delete_by_datetime(None, None)
    state = prepared.heat_pumps[0]
    assert state.cooling_season is True
    assert state.dewpoint_c == pytest.approx(17.5)


def test_cooling_settings_validation():
    with pytest.raises(ValueError):
        HeatPumpCommonSettings(device_id="x", cooling={"setpoint_c": 25.0, "max_c": 24.0})
    keys = device().measurement_keys
    assert "wbb12-cooling-season" in keys and "wbb12-dewpoint" in keys
    assert "wbb12-dewpoint" not in HeatPumpCommonSettings(device_id="wbb12").measurement_keys

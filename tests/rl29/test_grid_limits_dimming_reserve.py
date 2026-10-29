"""Grid connection limits, §14a dimming of batteries/EVs and the weather reserve (fork).

- ``optimization.grid.max_export_w``: PV above the feed-in cap is curtailed and earns
  nothing (Solarspitzengesetz 60 %), so the optimizer charges the battery at the peak.
- ``optimization.grid.max_import_w``: energy above the import cap is penalised.
- §14a: a controllable battery or EV reporting ``<id>-power-limited`` may draw only the
  dimming power from the grid for ``dimming_hold_hours``.
- Battery weather reserve: hold ``min_soc_percentage`` during a reported warning.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pendulum
import pytest
import pytest_asyncio

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.genetic.battery import (
    Battery,
    ElectricVehicleParameters,
    SolarPanelBatteryParameters,
)
from akkudoktoreos.devices.genetic.inverter import Inverter, InverterParameters
from akkudoktoreos.devices.settings.batterysettings import BatteriesCommonSettings
from akkudoktoreos.measurement.measurement import Measurement
from akkudoktoreos.optimization.genetic.genetic import (
    GeneticEnergyManagementParameters,
    GeneticOptimization,
    GeneticSimulation,
)

N = 6
TZ = "Europe/Berlin"


def simulate(pv=8000.0, load=500.0, dc=0.0, ac=0.0, ev_charge=0.0, **attrs):
    battery = Battery(
        SolarPanelBatteryParameters(
            device_id="b",
            capacity_wh=10000,
            initial_soc_percentage=10,
            min_soc_percentage=5,
            max_charge_power_w=5000,
        ),
        prediction_hours=N,
    )
    inverter = Inverter(
        InverterParameters(device_id="i", max_power_wh=20000, battery_id="b"), battery=battery
    )
    ev = Battery(
        ElectricVehicleParameters(
            device_id="ev", capacity_wh=40000, initial_soc_percentage=20, max_charge_power_w=11000
        ),
        prediction_hours=N,
    )
    sim = GeneticSimulation()
    sim.prepare(
        GeneticEnergyManagementParameters(
            pv_forecast_wh=[pv] * N,
            total_load=[load] * N,
            electricity_price_per_wh=[0.0003] * N,
            feed_in_tariff_per_wh=[0.00007] * N,
            price_per_wh_battery=0.0,
        ),
        optimization_hours=N,
        prediction_hours=N,
        inverter=inverter,
        ev=ev,
    )
    for name, value in attrs.items():
        setattr(sim, name, value)
    sim.reset()
    assert sim.dc_charge_hours is not None and sim.ac_charge_hours is not None
    assert sim.ev_charge_hours is not None
    sim.dc_charge_hours[:] = dc
    sim.ac_charge_hours[:] = ac
    sim.ev_charge_hours[:] = ev_charge
    return sim.simulate(0)


# ----------------------------------------------------------------------
# Grid connection limits
# ----------------------------------------------------------------------


def test_export_above_the_cap_is_curtailed():
    free = simulate()
    capped = simulate(max_grid_export_wh=4000.0)
    assert list(free["Netzeinspeisung_Wh_pro_Stunde"]) == [7500.0] * N
    assert list(capped["Netzeinspeisung_Wh_pro_Stunde"]) == [4000.0] * N
    assert capped["curtailed_wh"] == pytest.approx(3500.0 * N)
    assert capped["Gesamteinnahmen_Euro"] == pytest.approx(4000.0 * N * 0.00007)


def test_charging_the_battery_at_the_peak_avoids_curtailment():
    """With the cap, storing PV is worth more than feeding it in."""
    idle = simulate(max_grid_export_wh=4000.0)
    charge = simulate(dc=1.0, max_grid_export_wh=4000.0)
    assert charge["curtailed_wh"] < idle["curtailed_wh"]
    assert charge["akku_soc_pro_stunde"][-1] > idle["akku_soc_pro_stunde"][-1]


def test_import_above_the_cap_is_penalised():
    result = simulate(pv=0.0, load=12000.0, max_grid_import_wh=10000.0)
    assert result["grid_limit_penalty_amt"] == pytest.approx(2000.0 * N * 0.001)
    assert simulate(pv=0.0, load=12000.0)["grid_limit_penalty_amt"] == 0.0


# ----------------------------------------------------------------------
# §14a dimming
# ----------------------------------------------------------------------


def test_battery_grid_charging_is_dimmed_in_the_first_slots():
    normal = simulate(pv=0.0, ac=1.0)
    dimmed = simulate(pv=0.0, ac=1.0, battery_dimmed_wh=1000.0, battery_dimmed_slots=2)
    grid = dimmed["Netzbezug_Wh_pro_Stunde"]
    assert normal["Netzbezug_Wh_pro_Stunde"][0] > 5000.0
    assert grid[0] <= 500.0 + 1000.0 + 1e-6 and grid[1] <= 1500.0 + 1e-6
    assert grid[2] > 5000.0  # dimming over: full charging power again


def test_ev_charging_is_dimmed_in_the_first_slots():
    normal = simulate(pv=0.0, ev_charge=1.0)
    dimmed = simulate(pv=0.0, ev_charge=1.0, ev_dimmed_wh=4200.0, ev_dimmed_slots=1)
    assert normal["Netzbezug_Wh_pro_Stunde"][0] > 10000.0
    assert dimmed["Netzbezug_Wh_pro_Stunde"][0] == pytest.approx(500.0 + 4200.0, rel=0.01)
    assert dimmed["Netzbezug_Wh_pro_Stunde"][1] > 10000.0


# ----------------------------------------------------------------------
# Run configuration and reserve penalty
# ----------------------------------------------------------------------


def _optimizer(config_eos: ConfigEOS, slot_h: float = 1.0) -> GeneticOptimization:
    optimizer = GeneticOptimization.__new__(GeneticOptimization)
    object.__setattr__(optimizer, "simulation", GeneticSimulation())
    return optimizer


def test_configure_grid_control(config_eos: ConfigEOS, monkeypatch):
    config_eos.merge_settings_from_dict(
        {"optimization": {"grid": {"max_export_w": 6000, "max_import_w": 11000}}}
    )
    start = pendulum.datetime(2026, 11, 3, 10, 0, tz=TZ)
    battery = SolarPanelBatteryParameters(
        device_id="b",
        capacity_wh=10000,
        power_limited_w=4200.0,
        power_limited_hours=1.0,
        reserve_min_soc_percentage=50,
        reserve_start=start.add(hours=3),
        reserve_end=start.add(hours=6),
    )
    ev = ElectricVehicleParameters(device_id="ev", capacity_wh=40000)
    optimizer = _optimizer(config_eos)
    monkeypatch.setattr(GeneticOptimization, "slot_duration_h", property(lambda self: 0.25))
    monkeypatch.setattr(GeneticOptimization, "control_end_slot", property(lambda self: 96))
    optimizer._slot0_datetime = start
    optimizer._configure_grid_control(SimpleNamespace(pv_battery=battery, ev=ev))  # type: ignore[arg-type]
    sim = optimizer.simulation
    assert sim.max_grid_export_wh == pytest.approx(1500.0)
    assert sim.max_grid_import_wh == pytest.approx(2750.0)
    assert sim.battery_dimmed_wh == pytest.approx(1050.0) and sim.battery_dimmed_slots == 4
    assert sim.ev_dimmed_wh is None and sim.ev_dimmed_slots == 0
    assert optimizer._reserve_window == (12, 23, 50.0)


def test_reserve_penalty_counts_the_largest_shortfall(config_eos: ConfigEOS):
    optimizer = _optimizer(config_eos)
    optimizer._reserve_window = (2, 3, 50.0)
    result = {"akku_soc_pro_stunde": [80.0, 60.0, 45.0, 30.0, 70.0]}
    assert optimizer._reserve_penalty(result, 0) == pytest.approx(20.0 * 10)
    result = {"akku_soc_pro_stunde": [80.0, 60.0, 55.0, 52.0, 70.0]}
    assert optimizer._reserve_penalty(result, 0) == 0.0
    optimizer._reserve_window = None
    assert optimizer._reserve_penalty(result, 0) == 0.0


def test_measurement_keys():
    plain = BatteriesCommonSettings(device_id="b")
    assert "b-power-limited" in plain.measurement_keys
    assert "b-warning-start-ts" not in plain.measurement_keys
    reserve = BatteriesCommonSettings.model_validate(
        {"device_id": "b", "weather_reserve": {"enabled": True}}
    )
    assert {"b-warning-start-ts", "b-warning-end-ts"} <= set(reserve.measurement_keys)


def test_milp_uses_the_shared_import_limit(config_eos: ConfigEOS):
    from akkudoktoreos.optimization.milp.milpoptimization import MilpOptimization

    config_eos.merge_settings_from_dict({"optimization": {"grid": {"max_import_w": 9000}}})
    optimizer = MilpOptimization.__new__(MilpOptimization)
    assert optimizer._import_limit_w() == 9000
    config_eos.merge_settings_from_dict({"optimization": {"milp": {"max_grid_import_w": 7000}}})
    assert optimizer._import_limit_w() == 7000


# ----------------------------------------------------------------------
# Request: dimming signal and warning from measurements
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated_measurement(config_eos: ConfigEOS) -> AsyncGenerator[Measurement, None]:
    measurement = get_measurement()
    await measurement.delete_by_datetime(None, None)
    try:
        yield measurement
    finally:
        await measurement.delete_by_datetime(None, None)


@pytest.mark.asyncio
async def test_request_reads_dimming_and_reserve(
    config_eos: ConfigEOS, isolated_measurement: Measurement
):
    from akkudoktoreos.optimization.genetic.configrequest import (
        ConfigOptimizationRequest,
    )

    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "prediction": {"hours": 48},
            "elecfee": {
                "grid_control_14a": {
                    "module": "module1",
                    "controllable_device_ids": ["bat1", "car1"],
                }
            },
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
                "max_batteries": 1,
                "batteries": {"bat1": {"capacity_wh": 10000, "weather_reserve": {"enabled": True}}},
                "max_electric_vehicles": 1,
                "electric_vehicles": {"car1": {"capacity_wh": 40000}},
                "max_inverters": 1,
                "inverters": {"inv": {"max_power_w": 10000, "battery_id": "bat1"}},
                "max_home_appliances": 0,
                "home_appliances": {},
                "max_heat_pumps": 0,
                "heat_pumps": {},
            },
        }
    )
    now = pendulum.datetime(2026, 11, 20, 9, 7, tz=TZ)
    get_ems(init=True).set_start_datetime(now, interval_seconds=3600)
    for key in ("bat1-soc-factor", "car1-soc-factor"):
        await isolated_measurement.update_value(now, key, 0.5)

    def request() -> ConfigOptimizationRequest:
        return ConfigOptimizationRequest.model_validate(
            {
                "forecasts": {
                    "pv_forecast_wh": [0.0] * 48,
                    "total_load": [300.0] * 48,
                    "electricity_price_per_wh": [0.0003] * 48,
                    "feed_in_tariff_per_wh": [0.00007] * 48,
                    "temperature_forecast": [4.0] * 48,
                }
            }
        )

    params = await request().resolve()
    assert params.pv_battery is not None and params.ev is not None
    assert params.pv_battery.power_limited_w is None and params.ev.power_limited_w is None
    assert params.pv_battery.reserve_start is None

    await isolated_measurement.update_value(now, "car1-power-limited", 1.0)
    begin, end = now.add(hours=4), now.add(hours=9)
    await isolated_measurement.update_value(now, "bat1-warning-start-ts", begin.timestamp())
    await isolated_measurement.update_value(now, "bat1-warning-end-ts", end.timestamp())
    params = await request().resolve()
    assert params.ev is not None and params.ev.power_limited_w == 4200.0
    assert params.pv_battery is not None and params.pv_battery.power_limited_w is None
    assert params.pv_battery.reserve_min_soc_percentage == 50
    assert params.pv_battery.reserve_start is not None
    assert params.pv_battery.reserve_start.timestamp() == pytest.approx(begin.timestamp())

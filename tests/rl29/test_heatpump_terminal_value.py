"""Value of the heat stored at the end of the horizon (RL29 fork).

Stored heat displaces heat the optimizer would produce in the cheapest hours,
so it is valued at the cheapest share of the marginal prices; hours with PV
surplus count with the feed-in tariff. Hot water above the heat pump setpoint
(heating rod) loses more until it is tapped and is valued with a discount.
"""

from __future__ import annotations

import math

import pendulum
import pytest

from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import DhwMode, HeatingMode
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    TANK_DWELL_HOURS,
    GeneticHeatPumpParameters,
    build_run_plan,
    terminal_price_per_wh,
)

T0 = pendulum.datetime(2026, 11, 3, 10, 0, tz="Europe/Berlin")
PRICES = [0.00040] * 6 + [0.00020] * 6 + [0.00030] * 12  # expensive, cheap, normal


def plan(prices=PRICES, surplus=None, feed_in=None, initial_dhw=45.0, **settings):
    device = HeatPumpCommonSettings(
        device_id="wbb12", dhw={"rod_power_w": 3000.0, "setpoint_c": 45.0}, **settings
    )
    params = GeneticHeatPumpParameters(
        device_id="wbb12", settings=device, initial_room_c=21.0, initial_dhw_c=initial_dhw
    )
    starts = [T0.add(hours=i) for i in range(24)]
    return build_run_plan(params, starts, 1.0, [2.0] * 24, [0.0] * 24, prices, surplus, feed_in)


class TestTerminalPrice:
    def test_cheapest_share_of_the_horizon(self):
        assert terminal_price_per_wh(PRICES, 0.25) == pytest.approx(0.00020)
        assert terminal_price_per_wh(PRICES, 1.0) == pytest.approx(sum(PRICES) / 24)
        assert terminal_price_per_wh(PRICES, 0.5) == pytest.approx((6 * 0.00020 + 6 * 0.00030) / 12)

    def test_surplus_hours_count_with_the_feed_in_tariff(self):
        surplus = [0.0] * 12 + [3000.0] * 6 + [0.0] * 6
        feed_in = [0.00008] * 24
        value = terminal_price_per_wh(PRICES, 0.25, surplus, feed_in, surplus_needed_w=1000.0)
        assert value == pytest.approx(0.00008)
        # too little surplus for the heat pump: no change
        weak = [500.0] * 24
        assert terminal_price_per_wh(PRICES, 0.25, weak, feed_in, 1000.0) == pytest.approx(0.0002)
        # a negative feed-in tariff (negative prices) does not lift the value
        assert terminal_price_per_wh(PRICES, 0.25, surplus, [-0.00001] * 24, 1000.0) < 0

    def test_missing_values_are_skipped(self):
        assert terminal_price_per_wh([None, math.nan, 0.0003], 0.25) == pytest.approx(0.0003)
        assert terminal_price_per_wh([], 0.25) == 0.0


class TestStoredHeatValue:
    def test_default_values_use_the_cheap_hours(self):
        cheap = plan()
        mean = plan(optimization={"stored_heat_price_quantile": 1.0})
        assert cheap.building_heat_value < mean.building_heat_value
        assert cheap.tank_heat_value < mean.tank_heat_value
        assert cheap.building_heat_value == pytest.approx(
            mean.building_heat_value * 0.0002 / (sum(PRICES) / 24)
        )

    def test_pv_surplus_lowers_the_value_of_stored_heat(self):
        dull = plan(surplus=[0.0] * 24, feed_in=[0.00008] * 24)
        sunny = plan(surplus=[0.0] * 12 + [4000.0] * 8 + [0.0] * 4, feed_in=[0.00008] * 24)
        assert sunny.building_heat_value < dull.building_heat_value
        assert sunny.tank_heat_value == pytest.approx(dull.tank_heat_value * 0.00008 / 0.0002)

    def test_module_2_meter_ignores_pv_surplus(self):
        device = HeatPumpCommonSettings(device_id="wbb12", dhw={"setpoint_c": 45.0})
        params = GeneticHeatPumpParameters(
            device_id="wbb12",
            settings=device,
            initial_room_c=21.0,
            initial_dhw_c=45.0,
            separate_meter_price_reduction_amt_wh=0.00005,
        )
        starts = [T0.add(hours=i) for i in range(24)]
        separate = build_run_plan(
            params, starts, 1.0, [2.0] * 24, [0.0] * 24, PRICES, [5000.0] * 24, [0.00008] * 24
        )
        assert separate.building_heat_value == pytest.approx(
            (0.0002 - 0.00005) / (0.0002 / plan().building_heat_value)
        )

    def test_tank_heat_above_the_setpoint_is_discounted(self):
        run = plan()
        assert run.tank_heat_value_above_setpoint is not None
        tank = run.system.tank
        retention = math.exp(-tank.params.loss_w_per_k * TANK_DWELL_HOURS / tank.capacity)
        assert 0.9 < retention < 1.0
        assert run.tank_heat_value_above_setpoint == pytest.approx(run.tank_heat_value * retention)

    def test_rod_heating_at_the_end_earns_less_credit_than_the_energy_it_stores(self):
        run = plan()
        hold = run.evaluate(run.genes_for_modes([HeatingMode.NORMAL] * 24, [DhwMode.HOLD] * 24))
        rod = run.genes_for_modes(
            [HeatingMode.NORMAL] * 24, [DhwMode.HOLD] * 22 + [DhwMode.ROD] * 2
        )
        heated = run.evaluate(rod)
        assert heated.stored_heat_credit > hold.stored_heat_credit
        # The rod turns 1 Wh electric into 1 Wh heat above the setpoint; that heat
        # earns less than the full tank value (loss discount), so the credit of
        # the extra energy stays below electric energy x tank value.
        extra_electric_wh = sum(heated.electric_wh) - sum(hold.electric_wh)
        assert heated.stored_heat_credit - hold.stored_heat_credit < (
            extra_electric_wh * run.tank_heat_value
        )
        assert heated.stored_heat_credit - hold.stored_heat_credit > (
            0.7 * extra_electric_wh * run.tank_heat_value_above_setpoint  # minus losses, tapping
        )


class TestBatteryTakesSurplusFirst:
    def test_surplus_the_battery_takes_is_no_free_heat(self):
        surplus = [0.0] * 12 + [3000.0] * 6 + [0.0] * 6
        feed_in = [0.00008] * 24
        # 6 h x 3 kWh surplus; a 10 kWh battery takes 3 full hours, the 4th fills it
        value = terminal_price_per_wh(
            PRICES, 0.25, surplus, feed_in, surplus_needed_w=1000.0, battery_absorb_wh=10000.0
        )
        # cheapest quarter: 3 free hours at the tariff and 3 cheap hours at 0.0002
        assert value == pytest.approx((3 * 0.00008 + 3 * 0.00020) / 6)
        everything = terminal_price_per_wh(
            PRICES, 0.25, surplus, feed_in, 1000.0, battery_absorb_wh=100000.0
        )
        assert everything == pytest.approx(0.00020)

    def test_the_battery_absorbs_again_every_day(self):
        surplus = [3000.0] * 48
        feed_in = [0.00008] * 48
        prices = [0.0003] * 48
        value = terminal_price_per_wh(
            prices, 1.0, surplus, feed_in, 1000.0, battery_absorb_wh=6000.0, slot_hours=1.0
        )
        # 2 hours per day go to the battery, 22 are free
        assert value == pytest.approx((4 * 0.0003 + 44 * 0.00008) / 48)


class TestNextDayValuesTheStoredHeat:
    def tail_plan(self, tail_outdoor=None, tail_prices=None):
        device = HeatPumpCommonSettings(device_id="wbb12", dhw={"setpoint_c": 45.0})
        params = GeneticHeatPumpParameters(
            device_id="wbb12", settings=device, initial_room_c=21.0, initial_dhw_c=45.0
        )
        starts = [T0.add(hours=i) for i in range(24)]
        return build_run_plan(
            params,
            starts,
            1.0,
            [2.0] * 24,
            [0.0] * 24,
            PRICES,
            tail_outdoor_c=tail_outdoor,
            tail_price_per_wh=tail_prices,
        )

    def test_a_cold_next_day_makes_stored_heat_worth_more(self):
        same = self.tail_plan()
        cold = self.tail_plan(tail_outdoor=[-8.0] * 24)
        mild = self.tail_plan(tail_outdoor=[12.0] * 24)
        assert cold.building_heat_value > same.building_heat_value > mild.building_heat_value

    def test_prices_after_the_horizon_value_the_stored_heat(self):
        same = self.tail_plan()
        dear = self.tail_plan(tail_prices=[0.0004] * 24)
        assert dear.building_heat_value == pytest.approx(same.building_heat_value * 2.0)

    def test_a_short_tail_keeps_the_horizon_prices(self):
        same = self.tail_plan()
        short = self.tail_plan(tail_prices=[0.0004] * 3)
        assert short.building_heat_value == pytest.approx(same.building_heat_value)


def test_tail_consumption_follows_the_outdoor_temperature():
    from akkudoktoreos.optimization.genetic.heatpumpgenes import tail_electric_wh

    run = plan()
    device = HeatPumpCommonSettings(
        device_id="wbb12", dhw={"rod_power_w": 3000.0, "setpoint_c": 45.0}
    )
    starts = [T0.add(hours=24 + i) for i in range(4)]
    cold = tail_electric_wh(run, device, starts, [-5.0] * 4, 1.0)
    mild = tail_electric_wh(run, device, starts, [10.0] * 4, 1.0)
    assert len(cold) == 4
    assert all(c > m > 0 for c, m in zip(cold, mild))
    # missing values carry the last known temperature forward
    assert tail_electric_wh(run, device, starts, [-5.0, None, None, None], 1.0) == pytest.approx(
        cold
    )


@pytest.mark.asyncio
async def test_battery_outlook_includes_the_heat_pump_after_the_horizon(config_eos):
    from akkudoktoreos.core.coreabc import get_ems
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import GeneticOptimizationParameters
    from akkudoktoreos.utils.datetimeutil import to_datetime

    tz = "Europe/Berlin"
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": tz},
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {
                    "horizon_hours": 12,
                    "tail_horizon_hours": 24,
                    "interval_sec": 3600,
                    "individuals": 10,
                    "generations": 10,
                    "terminal_value_mode": "AUTO",
                }
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-11-02T00:00:00", in_timezone=tz))
    hours = 48
    device = HeatPumpCommonSettings(device_id="hp1", dhw={"setpoint_c": 45.0})
    params = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * hours,
                "gesamtlast": [300.0] * hours,
                "strompreis_euro_pro_wh": [0.0003] * hours,
                "einspeiseverguetung_euro_pro_wh": [0.00007] * hours,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "pv_battery": {"device_id": "bat", "capacity_wh": 10000, "initial_soc_percentage": 50},
            "inverter": {"device_id": "inv", "battery_id": "bat", "max_power_wh": 10000},
            "forecast_interval_seconds": 3600,
            "temperature_forecast": [2.0] * 12 + [-6.0] * 36,
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "hp1",
                    "settings": device.model_dump(),
                    "initial_room_c": 21.0,
                    "initial_dhw_c": 45.0,
                }
            ],
        }
    )
    optimizer = GeneticOptimization(fixed_seed=3)
    optimizer.optimize_ems(params, ngen=2, individuals=8)
    outlook = optimizer._hp_outlook_wh
    assert outlook is not None
    # status quo in the horizon, steady demand at the colder tail temperature after it
    assert outlook[:12].sum() > 0
    assert outlook[12:36].mean() > outlook[:12].mean()

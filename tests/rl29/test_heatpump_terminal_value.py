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

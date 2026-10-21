"""§14a EnWG grid fee modules: none, module 1, module 2, module 1+3 (RL29 fork)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pendulum
import pytest

from akkudoktoreos.core.coreabc import get_ems, get_measurement
from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
from akkudoktoreos.devices.thermal.heatpump import DhwMode, HeatingMode
from akkudoktoreos.optimization.genetic.configrequest import ConfigOptimizationRequest
from akkudoktoreos.optimization.genetic.heatpumpgenes import (
    GeneticHeatPumpParameters,
    HeatPumpGeneLayout,
    build_run_plan,
)
from akkudoktoreos.prediction.elecfeefixed import ElecFeeFixed
from akkudoktoreos.prediction.gridcontrol14a import GridControl14aSettings
from akkudoktoreos.utils.datetimeutil import to_datetime

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rl29" / "gridfee_14a.py"
spec = importlib.util.spec_from_file_location("gridfee_14a", SCRIPT)
assert spec and spec.loader
script = importlib.util.module_from_spec(spec)
sys.modules["gridfee_14a"] = script
spec.loader.exec_module(script)

REGULAR = 0.075
MVV = script.module3_windows(0.0173, 0.0432, 0.0596)


def grid(module: str, **kwargs: Any) -> GridControl14aSettings:
    data: dict[str, Any] = {"module": module, "controllable_device_ids": ["wbb12"], **kwargs}
    if module in ("module2", "module1_3"):
        data.setdefault("regular_network_amt_kwh", REGULAR)
    if module == "module1_3":
        data.setdefault("module3_network_amt_kwh", MVV)
    return GridControl14aSettings.model_validate(data)


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


class TestSettings:
    def test_default_is_no_participation(self):
        settings = GridControl14aSettings()
        assert settings.module == "none"
        assert not settings.dims
        assert not settings.is_controllable("wbb12")

    @pytest.mark.parametrize("module", ["module1", "module2", "module1_3"])
    def test_every_module_dims_controllable_devices(self, module):
        settings = grid(module)
        assert settings.is_controllable("wbb12")
        assert not settings.is_controllable("dishwasher")

    def test_only_module2_meters_separately(self):
        assert grid("module2").separately_metered("wbb12")
        for module in ("none", "module1", "module1_3"):
            assert not grid(module).separately_metered("wbb12")

    def test_module2_reduction(self):
        assert grid("module2").module2_reduction_amt_kwh() == pytest.approx(REGULAR * 0.6)
        assert grid("module1").module2_reduction_amt_kwh() == 0.0
        assert grid("module2", module2_reduction_percent=40).module2_reduction_amt_kwh() == (
            pytest.approx(REGULAR * 0.4)
        )

    @pytest.mark.parametrize(
        ("when", "band"),
        [
            ("2026-01-15T03:00", 0.0173),
            ("2026-11-15T12:00", 0.0432),
            ("2026-11-15T18:30", 0.0596),
            ("2026-05-15T03:00", None),
            ("2026-07-15T18:30", None),
        ],
    )
    def test_module3_delta(self, when, band):
        moment = pendulum.parse(when, tz="Europe/Berlin")
        expected = 0.0 if band is None else band - REGULAR
        assert grid("module1_3").module3_delta_amt_kwh(moment) == pytest.approx(expected)
        assert grid("module1").module3_delta_amt_kwh(moment) == 0.0

    @pytest.mark.parametrize(
        "data",
        [
            {"module": "module2"},
            {"module": "module1_3", "regular_network_amt_kwh": REGULAR},
            {"module": "module4"},
        ],
    )
    def test_incomplete_settings_rejected(self, data):
        with pytest.raises(ValueError):
            GridControl14aSettings.model_validate(data)

    def test_part_of_the_fee_configuration(self, config_eos):
        config_eos.merge_settings_from_dict(
            {"elecfee": {"grid_control_14a": grid("module1").model_dump()}}
        )
        assert config_eos.elecfee.grid_control_14a.module == "module1"


# ----------------------------------------------------------------------
# Module 3 in the fixed fee provider
# ----------------------------------------------------------------------


@pytest.fixture
def fee_provider(config_eos):
    config_eos.merge_settings_from_dict(
        {
            "elecfee": {
                "provider": "ElecFeeFixed",
                "elecfeefixed": {
                    "consumption_amt_kwh": {
                        "windows": [
                            {"start_time": "00:00", "duration": "24 hours", "value": 0.13796}
                        ]
                    },
                    "consumption_percent_amt": {
                        "windows": [{"start_time": "00:00", "duration": "24 hours", "value": 19.0}]
                    },
                },
            }
        }
    )
    provider = ElecFeeFixed()
    provider._db_reset_state()
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "start", "expected"),
    [
        # Module 1+3 in January: bands replace the regular network price.
        ("module1_3", "2026-01-15 00:00:00", {0: 0.0432, 3: 0.0173, 12: 0.0432, 18: 0.0596}),
        # Module 1+3 in May: regular price all day.
        ("module1_3", "2026-05-15 00:00:00", {0: REGULAR, 3: REGULAR, 18: REGULAR}),
        # Module 1 and none: regular price.
        ("module1", "2026-01-15 00:00:00", {3: REGULAR, 18: REGULAR}),
        ("none", "2026-01-15 00:00:00", {3: REGULAR, 18: REGULAR}),
    ],
)
async def test_fixed_fees_follow_the_module(config_eos, fee_provider, module, start, expected):
    config_eos.elecfee.grid_control_14a = grid(module)
    get_ems().set_start_datetime(to_datetime(start, in_timezone="Europe/Berlin"))
    config_eos.prediction.hours = 24
    await fee_provider.update_data(force_enable=True, force_update=True)
    by_hour = {
        record.date_time.in_timezone("Europe/Berlin").hour: record.elecfee_consumption_amt_wh
        for record in fee_provider.records
        if record.date_time.minute == 0
    }
    for hour, network in expected.items():
        assert by_hour[hour] == pytest.approx((0.13796 - REGULAR + network) / 1000.0), hour


# ----------------------------------------------------------------------
# Heat pump: dimming and module 2
# ----------------------------------------------------------------------

T0 = pendulum.datetime(2026, 11, 3, 10, 0, tz="Europe/Berlin")


def plan(**params: Any):
    device = HeatPumpCommonSettings(device_id="wbb12")
    parameters = GeneticHeatPumpParameters(
        device_id="wbb12", settings=device, initial_room_c=21.0, initial_dhw_c=45.0, **params
    )
    starts = [T0.add(hours=i) for i in range(12)]
    return build_run_plan(parameters, starts, 1.0, [-8.0] * 12, None, [0.0003] * 12)


def test_dimming_limits_the_first_hours():
    dimmed = plan(power_limited_w=1500.0, power_limited_hours=2.0)
    assert [i.max_electric_w for i in dimmed.inputs[:3]] == [1500.0, 1500.0, None]
    genes = dimmed.genes_for_modes([HeatingMode.RAISED] * 12, [DhwMode.HEAT_PUMP] * 12)
    outcome = dimmed.evaluate(genes)
    assert max(outcome.electric_wh[:2]) <= 1500.0 + 1e-6
    assert outcome.electric_wh[2] > 1500.0


def test_module2_device_is_not_in_the_house_balance():
    shared = plan()
    separate = plan(separate_meter_price_reduction_amt_wh=0.00005)
    genes = shared.default_genes()
    shared_load, shared_extra = HeatPumpGeneLayout([shared]).evaluate(genes)
    separate_load, separate_extra = HeatPumpGeneLayout([separate]).evaluate(genes)
    assert shared_load is not None and sum(shared_load) > 0
    assert separate_load == [0.0] * 12
    energy = sum(separate.evaluate(genes).electric_wh)
    cost = separate.evaluate(genes).separate_meter_cost
    assert cost == pytest.approx(energy * (0.0003 - 0.00005))
    assert separate_extra - shared_extra == pytest.approx(cost, rel=0.2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "limited", "dimmed", "separate"),
    [
        ("none", 1.0, False, False),
        ("module1", 0.0, False, False),
        ("module1", 1.0, True, False),
        ("module2", 1.0, True, True),
        ("module1_3", 1.0, True, False),
    ],
)
async def test_config_request_applies_the_module(config_eos, module, limited, dimmed, separate):
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
                "heat_pumps": {"wbb12": {}},
            },
            "elecfee": {
                "elecfeefixed": {
                    "consumption_percent_amt": {
                        "windows": [{"start_time": "00:00", "duration": "24 hours", "value": 19.0}]
                    }
                },
                "grid_control_14a": grid(module).model_dump(),
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-11-03T00:00:00"))
    now = get_ems().start_datetime
    await measurement.update_value(now.subtract(minutes=2), "wbb12-power-limited", limited)
    try:
        prepared = await ConfigOptimizationRequest.model_validate(
            {
                "forecasts": {
                    "pv_forecast_wh": [0.0] * 48,
                    "total_load": [300.0] * 48,
                    "electricity_price_per_wh": [0.0003] * 48,
                    "feed_in_tariff_per_wh": [0.00007] * 48,
                    "temperature_forecast": [3.0] * 48,
                }
            }
        ).resolve()
    finally:
        await measurement.delete_by_datetime(None, None)
    assert prepared.heat_pumps is not None
    state = prepared.heat_pumps[0]
    assert (state.power_limited_w == 4200.0) is dimmed
    if separate:
        assert state.separate_meter_price_reduction_amt_wh == pytest.approx(
            REGULAR * 0.6 * 1.19 / 1000.0
        )
    else:
        assert state.separate_meter_price_reduction_amt_wh is None


# ----------------------------------------------------------------------
# Script
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--module", "none"],
        ["--module", "module1", "--device", "wbb12"],
        ["--module", "module2", "--device", "wbb12", "--regular-network-amt-kwh", "0.075"],
        ["--module", "module1_3", "--device", "wbb12", "--regular-network-amt-kwh", "0.075"],
    ],
)
def test_script_snippet_is_valid_config(config_eos, capsys, argv):
    assert script.main(argv) == 0
    data = json.loads(capsys.readouterr().out)
    config_eos.merge_settings_from_dict(data)
    assert config_eos.elecfee.grid_control_14a.module == argv[1]


def test_script_requires_the_regular_price():
    with pytest.raises(SystemExit):
        script.main(["--module", "module2"])

#!/usr/bin/env python3
"""Compare MILP and GENETIC on the same synthetic day (RL29 fork).

Quarter-hour slots, 24 h horizon, 11 kWh battery, heat pump, washing machine, a PV day
and a price curve with a morning and an evening peak. Both results are scored by the
same simulation (the GENETIC fitness), so the numbers are directly comparable.

    uv run python scripts/rl29/compare_milp_genetic.py [--generations 400] [--individuals 300]
"""

from __future__ import annotations

import argparse
import math
import tempfile
import time

from deap import creator


def scenario(slots: int):
    from akkudoktoreos.devices.settings.heatpumpsettings import HeatPumpCommonSettings
    from akkudoktoreos.optimization.genetic.geneticparams import (
        GeneticOptimizationParameters,
    )

    hour = [s / 4.0 for s in range(slots)]
    pv = [
        max(0.0, 18000 * math.sin(math.pi * (h - 7.5) / 10.0)) * 0.25 * 0.6
        if 7.5 <= h <= 17.5
        else 0.0
        for h in hour
    ]
    load = [
        (350 + (900 if 17 <= h < 22 else 0) + (500 if 6.5 <= h < 8 else 0)) * 0.25 for h in hour
    ]
    price = [
        (
            0.24
            + 0.10 * math.exp(-((h - 8) ** 2) / 2)
            + 0.14 * math.exp(-((h - 19) ** 2) / 3)
            - 0.08 * math.exp(-((h - 13) ** 2) / 4)
        )
        / 1000
        for h in hour
    ]
    device = HeatPumpCommonSettings(device_id="wbb12", dhw={"rod_power_w": 3000})
    return GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": pv,
                "gesamtlast": load,
                "strompreis_euro_pro_wh": price,
                "einspeiseverguetung_euro_pro_wh": [0.000072] * slots,
                "preis_euro_pro_wh_akku": 0.0001,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 20000, "battery_id": "bat"},
            "forecast_interval_seconds": 900,
            "temperature_forecast": [3.0 + 4 * math.sin(math.pi * (h - 9) / 12) for h in hour],
            "ghi_forecast": [
                max(0.0, 400 * math.sin(math.pi * (h - 7.5) / 10.0)) if 7.5 <= h <= 17.5 else 0.0
                for h in hour
            ],
            "pv_battery": {
                "device_id": "bat",
                "capacity_wh": 11000,
                "initial_soc_percentage": 30,
                "min_soc_percentage": 5,
                "max_charge_power_w": 5000,
                "charging_efficiency": 0.95,
                "discharging_efficiency": 0.95,
                "charge_rates": [0.25, 0.5, 0.75, 1.0],
                "levelized_cost_of_storage_kwh": 0.02,
            },
            "ev": None,
            "heat_pumps": [
                {
                    "device_id": "wbb12",
                    "settings": device,
                    "initial_room_c": 21.0,
                    "initial_dhw_c": 44.0,
                }
            ],
            "home_appliances": [{"device_id": "washer", "consumption_wh": 1200, "duration_h": 2}],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generations", type=int, default=400)
    parser.add_argument("--individuals", type=int, default=300)
    parser.add_argument("--polish", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    import os

    # Keep the run's configuration out of the working directory.
    tmp = tempfile.mkdtemp()
    os.environ["EOS_DIR"] = tmp
    os.environ["EOS_CONFIG_DIR"] = tmp

    from akkudoktoreos.core.coreabc import get_config, get_ems, singletons_init
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.milp.milpoptimization import MilpOptimization
    from akkudoktoreos.utils.datetimeutil import to_datetime

    config = get_config(init=True)
    singletons_init()
    config.merge_settings_from_dict(
        {
            "general": {"data_folder_path": tmp, "timezone_override": "Europe/Berlin"},
            "prediction": {"hours": 48},
            "optimization": {
                "genetic": {
                    "horizon_hours": 24,
                    "tail_horizon_hours": 0,
                    "interval_sec": 900,
                    "terminal_value_mode": "FIXED",
                },
                "milp": {"polish_generations": args.polish},
            },
        }
    )
    get_ems().set_start_datetime(to_datetime("2026-11-03T00:00:00", in_timezone="Europe/Berlin"))
    slots = 96
    params = scenario(slots)

    results = {}
    for name, cls, ngen, individuals in (
        ("GENETIC", GeneticOptimization, args.generations, args.individuals),
        ("MILP", MilpOptimization, args.polish, 60),
    ):
        optimizer = cls(fixed_seed=args.seed)
        started = time.perf_counter()
        solution = optimizer.optimize_ems(params, ngen=ngen, individuals=individuals)
        elapsed = time.perf_counter() - started
        fit = optimizer.evaluate(
            creator.Individual(list(solution.start_solution)), params, 0, False
        )[0]
        results[name] = (fit, elapsed, solution)
        print(
            f"{name:8s} fitness {fit:8.4f} EUR  costs {solution.result.Gesamtkosten_Euro:7.3f}  "
            f"revenue {solution.result.Gesamteinnahmen_Euro:6.3f}  time {elapsed:6.1f} s"
        )
    diff = results["GENETIC"][0] - results["MILP"][0]
    print(
        f"MILP better by {diff:.4f} EUR ({diff / max(abs(results['GENETIC'][0]), 1e-9):.1%}), "
        f"{results['GENETIC'][1] / max(results['MILP'][1], 1e-9):.1f}x faster"
    )


if __name__ == "__main__":
    main()

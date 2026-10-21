#!/usr/bin/env python3
"""Configuration snippet for the §14a EnWG grid fee module (RL29 fork).

Selects the module (none, module1, module2, module1_3), the controllable
devices and, for module 3, the network price bands. The consumption fee in
``elecfee.elecfeefixed.consumption_amt_kwh`` keeps the regular network price;
EOS adds the module 3 band difference per slot and reduces the price of a
module 2 device itself (see ``akkudoktoreos.prediction.gridcontrol14a``).

Defaults for module 3: MVV Netze 2026 (net EUR/kWh): low 0.0173 (02-06),
standard 0.0432 (00-02, 06-17, 20-24), high 0.0596 (17-20), in Q1 and Q4.

Usage::

    gridfee_14a.py --module module1_3 --regular-network-amt-kwh 0.075 --device wbb12
    gridfee_14a.py --module module2 --regular-network-amt-kwh 0.075 --device wbb12
    gridfee_14a.py ... --apply http://192.168.30.85:8503   # PUT into EOS and save
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any

MODULES = ("none", "module1", "module2", "module1_3")
MODUL3_MONTHS = [1, 2, 3, 10, 11, 12]

# (start, hours, band) - MVV Netze time bands
MVV_BANDS = [
    ("00:00", 2, "standard"),
    ("02:00", 4, "low"),
    ("06:00", 11, "standard"),
    ("17:00", 3, "high"),
    ("20:00", 4, "standard"),
]


def module3_windows(
    low_amt_kwh: float,
    standard_amt_kwh: float,
    high_amt_kwh: float,
    months: list[int] = MODUL3_MONTHS,
    bands: list[tuple[str, int, str]] = MVV_BANDS,
) -> dict[str, Any]:
    """Network price windows of module 3 [EUR/kWh, net]."""
    prices = {"low": low_amt_kwh, "standard": standard_amt_kwh, "high": high_amt_kwh}
    return {
        "windows": [
            {
                "start_time": start,
                "duration": f"{hours} hours",
                "months": list(months),
                "value": prices[band],
            }
            for start, hours, band in bands
        ]
    }


def snippet(args: argparse.Namespace) -> dict[str, Any]:
    section: dict[str, Any] = {
        "module": args.module,
        "controllable_device_ids": list(args.device or []),
        "dimming_power_w": args.dimming_power_w,
    }
    if args.regular_network_amt_kwh is not None:
        section["regular_network_amt_kwh"] = args.regular_network_amt_kwh
    if args.module == "module2":
        section["module2_reduction_percent"] = args.module2_reduction_percent
    if args.module == "module1_3":
        section["module3_network_amt_kwh"] = module3_windows(
            args.low_amt_kwh, args.standard_amt_kwh, args.high_amt_kwh, args.months
        )
    else:
        section["module3_network_amt_kwh"] = {"windows": []}
    return {"elecfee": {"grid_control_14a": section}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--module", choices=MODULES, required=True)
    parser.add_argument(
        "--device", action="append", help="Controllable device id (repeatable), e.g. wbb12."
    )
    parser.add_argument(
        "--regular-network-amt-kwh",
        type=float,
        help="Regular network energy price in the consumption fee, net [EUR/kWh].",
    )
    parser.add_argument("--module2-reduction-percent", type=float, default=60.0)
    parser.add_argument("--low-amt-kwh", type=float, default=0.0173)
    parser.add_argument("--standard-amt-kwh", type=float, default=0.0432)
    parser.add_argument("--high-amt-kwh", type=float, default=0.0596)
    parser.add_argument("--months", type=int, nargs="+", default=MODUL3_MONTHS)
    parser.add_argument("--dimming-power-w", type=float, default=4200.0)
    parser.add_argument("--apply", metavar="EOS_URL", help="PUT the snippet into this EOS server.")
    args = parser.parse_args(argv)
    if any(m < 1 or m > 12 for m in args.months):
        parser.error("--months must be 1..12")
    if args.module in ("module2", "module1_3") and args.regular_network_amt_kwh is None:
        parser.error(f"{args.module} needs --regular-network-amt-kwh")

    data = snippet(args)
    print(json.dumps(data, indent=2))
    if args.apply:
        base = args.apply.rstrip("/")
        request = urllib.request.Request(  # noqa: S310
            base + "/v1/config",
            data=json.dumps(data).encode(),
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            print(f"EOS answered {response.status}", file=sys.stderr)
        save = urllib.request.Request(base + "/v1/config/file", method="PUT")  # noqa: S310
        with urllib.request.urlopen(save, timeout=30) as response:  # noqa: S310
            print(f"EOS saved the configuration file: {response.status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

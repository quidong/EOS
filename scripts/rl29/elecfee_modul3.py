#!/usr/bin/env python3
"""Configuration snippet for §14a EnWG Modul 3 (time-variable grid fee), RL29 fork.

Modul 3 replaces the network energy price in some quarters by three time bands.
Outside those quarters the regular network energy price applies. All other
per-kWh components (levies, electricity tax, concession fee, energy supply) stay
the same, so the configured fee per kWh is::

    consumption_amt_kwh = other_components + network_price(time)

VAT is configured separately (``consumption_percent_amt``) and not part of the
values here. Modul 1 (flat reduction of the yearly network fee) does not depend
on the time of use and therefore does not change the optimization.

Defaults: MVV Netze 2026 (net, ct/kWh): low 1.73 (02-06), standard 4.32
(00-02, 06-17, 20-24), high 5.96 (17-20), applied in Q1 and Q4.

Usage::

    elecfee_modul3.py --other-amt-kwh 0.0948 --regular-network-amt-kwh 0.0432
    elecfee_modul3.py ... --apply http://192.168.30.85:8503   # PUT into EOS and save

The snippet merges into the EOS configuration (``PUT /v1/config``) or is used
as the ``elecfee`` section of EOS.config.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any

MODUL3_MONTHS = [1, 2, 3, 10, 11, 12]
REGULAR_MONTHS = [4, 5, 6, 7, 8, 9]

# (start, hours, band)
MVV_BANDS = [
    ("00:00", 2, "standard"),
    ("02:00", 4, "low"),
    ("06:00", 11, "standard"),
    ("17:00", 3, "high"),
    ("20:00", 4, "standard"),
]


def consumption_windows(
    other_amt_kwh: float,
    regular_network_amt_kwh: float,
    low_amt_kwh: float,
    standard_amt_kwh: float,
    high_amt_kwh: float,
    modul3_months: list[int] = MODUL3_MONTHS,
) -> dict[str, Any]:
    """Time windows of the per-kWh consumption fee [currency/kWh, net]."""
    prices = {"low": low_amt_kwh, "standard": standard_amt_kwh, "high": high_amt_kwh}
    regular_months = [m for m in range(1, 13) if m not in modul3_months]
    windows: list[dict[str, Any]] = []
    for start, hours, band in MVV_BANDS:
        windows.append(
            {
                "start_time": start,
                "duration": f"{hours} hours",
                "months": list(modul3_months),
                "value": round(other_amt_kwh + prices[band], 6),
            }
        )
    if regular_months:
        windows.append(
            {
                "start_time": "00:00",
                "duration": "24 hours",
                "months": regular_months,
                "value": round(other_amt_kwh + regular_network_amt_kwh, 6),
            }
        )
    return {"windows": windows}


def snippet(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "elecfee": {
            "provider": "ElecFeeFixed",
            "elecfeefixed": {
                "consumption_amt_kwh": consumption_windows(
                    args.other_amt_kwh,
                    args.regular_network_amt_kwh,
                    args.low_amt_kwh,
                    args.standard_amt_kwh,
                    args.high_amt_kwh,
                    args.months,
                ),
                "consumption_percent_amt": {
                    "windows": [{"start_time": "00:00", "duration": "24 hours", "value": args.vat}]
                },
            },
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--other-amt-kwh",
        type=float,
        required=True,
        help="All per-kWh components except the network energy price, net [EUR/kWh].",
    )
    parser.add_argument(
        "--regular-network-amt-kwh",
        type=float,
        required=True,
        help="Regular network energy price outside the Modul 3 quarters, net [EUR/kWh].",
    )
    parser.add_argument("--low-amt-kwh", type=float, default=0.0173)
    parser.add_argument("--standard-amt-kwh", type=float, default=0.0432)
    parser.add_argument("--high-amt-kwh", type=float, default=0.0596)
    parser.add_argument("--months", type=int, nargs="+", default=MODUL3_MONTHS)
    parser.add_argument("--vat", type=float, default=19.0, help="VAT in percent.")
    parser.add_argument("--apply", metavar="EOS_URL", help="PUT the snippet into this EOS server.")
    args = parser.parse_args(argv)
    if any(m < 1 or m > 12 for m in args.months):
        parser.error("--months must be 1..12")

    data = snippet(args)
    print(json.dumps(data, indent=2))
    if args.apply:
        request = urllib.request.Request(  # noqa: S310
            args.apply.rstrip("/") + "/v1/config",
            data=json.dumps(data).encode(),
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            print(f"EOS answered {response.status}", file=sys.stderr)
        # Persist the merged configuration (EOS.config.json, previous file as backup).
        save = urllib.request.Request(args.apply.rstrip("/") + "/v1/config/file", method="PUT")  # noqa: S310
        with urllib.request.urlopen(save, timeout=30) as response:  # noqa: S310
            print(f"EOS saved the configuration file: {response.status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

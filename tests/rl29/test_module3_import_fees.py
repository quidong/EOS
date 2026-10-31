"""§14a module 3 with imported fees (fork).

The fixed fee provider applies the module 3 band price itself. Imported fees are
used as they are, unless ``elecfee.elecfeeimport.module3_included`` is False: then
EOS adds the band price minus the regular network price when computing the gross
electricity price, so module 3 works with any fee source.
"""

from __future__ import annotations

import pandas as pd
import pytest

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.prediction.elecpricefixed import ElecPriceFixed

TZ = "Europe/Berlin"
WINDOWS = [
    {"start_time": start, "end_time": end, "month": month, "value": value}
    for month in (1, 2, 3, 10, 11, 12)
    for start, end, value in (
        ("00:00", "02:00", 0.0432),
        ("02:00", "06:00", 0.0173),
        ("06:00", "17:00", 0.0432),
        ("17:00", "20:00", 0.0596),
        ("20:00", "00:00", 0.0432),
    )
]


def configure(config_eos: ConfigEOS, provider: str, included: bool) -> None:
    config_eos.merge_settings_from_dict(
        {
            "general": {"timezone": TZ},
            "elecfee": {
                "provider": provider,
                "elecfeeimport": {"module3_included": included},
                "grid_control_14a": {
                    "module": "module1_3",
                    "controllable_device_ids": ["hp1"],
                    "regular_network_amt_kwh": 0.0432,
                    "module3_network_amt_kwh": {"windows": WINDOWS},
                },
            },
        }
    )


def frame(hours: list[int], month: int = 11):
    index = pd.DatetimeIndex(
        [pd.Timestamp(2026, month, 3, h, tz=TZ) for h in hours], name="date_time"
    )
    raw = pd.Series([0.0001] * len(hours), index=index)
    fees = pd.DataFrame(
        {
            "elecfee_consumption_amt_wh": [0.0002] * len(hours),
            "elecfee_consumption_percent_amt": [0.0] * len(hours),
        },
        index=index,
    )
    return raw, fees


def test_import_without_module3_gets_the_band_price(config_eos: ConfigEOS):
    configure(config_eos, "ElecFeeImport", included=False)
    raw, fees = frame([1, 3, 18])
    gross = ElecPriceFixed()._compute_gross(raw, fees)
    # standard band = regular price, low band -2.59 ct, high band +1.64 ct (per kWh)
    assert gross.iloc[0] == pytest.approx(0.0003)
    assert gross.iloc[1] == pytest.approx(0.0003 + (0.0173 - 0.0432) / 1000)
    assert gross.iloc[2] == pytest.approx(0.0003 + (0.0596 - 0.0432) / 1000)


def test_import_with_module3_included_is_unchanged(config_eos: ConfigEOS):
    configure(config_eos, "ElecFeeImport", included=True)
    raw, fees = frame([3, 18])
    assert list(ElecPriceFixed()._compute_gross(raw, fees)) == pytest.approx([0.0003, 0.0003])


def test_fixed_provider_is_not_adjusted_twice(config_eos: ConfigEOS):
    configure(config_eos, "ElecFeeFixed", included=False)
    raw, fees = frame([3])
    assert ElecPriceFixed()._compute_gross(raw, fees).iloc[0] == pytest.approx(0.0003)


def test_quarters_without_module3_keep_the_regular_price(config_eos: ConfigEOS):
    configure(config_eos, "ElecFeeImport", included=False)
    raw, fees = frame([3, 18], month=6)
    assert list(ElecPriceFixed()._compute_gross(raw, fees)) == pytest.approx([0.0003, 0.0003])

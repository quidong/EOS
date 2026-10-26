"""Abstract and base classes for electricity price predictions.

Notes:
    - Ensure appropriate API keys or configurations are set up if required by external data sources.
"""

import json
import math
import time
from abc import abstractmethod
from pathlib import Path
from typing import Any, Callable, List, Optional

import numpy as np
import pandas as pd
from loguru import logger
from pydantic import Field, computed_field

from akkudoktoreos.prediction.predictionabc import PredictionRecord
from akkudoktoreos.prediction.priceabc import PricePredictionProviderBase


class ElecPriceDataRecord(PredictionRecord):
    """Represents a electricity price data record containing various price attributes at a specific datetime.

    Attributes:
        date_time (Optional[AwareDatetime]): The datetime of the record.

    """

    elecprice_marketprice_raw_wh: Optional[float] = Field(
        None,
        json_schema_extra={
            "description": "Raw electricity market price per Wh, always excluding fees [amount/Wh]"
        },
    )

    elecprice_marketprice_wh: Optional[float] = Field(
        None,
        json_schema_extra={
            "description": "Electricity market price per Wh, including fees if configured [amount/Wh]"
        },
    )

    # Computed fields
    @computed_field  # type: ignore[prop-decorator]
    @property
    def elecprice_marketprice_kwh(self) -> Optional[float]:
        """Electricity market price per kWh [amount/kWh].

        Convenience attribute calculated from `elecprice_marketprice_wh`.
        """
        if self.elecprice_marketprice_wh is None:
            return None
        return self.elecprice_marketprice_wh * 1000.0


class ElecPriceProvider(PricePredictionProviderBase[ElecPriceDataRecord]):
    """Abstract base class for electricity price providers.

    ElecPriceProvider is a thread-safe singleton, ensuring only one instance of this class is created.
    """

    # overload
    records: List[ElecPriceDataRecord] = Field(
        default_factory=list,
        json_schema_extra={"description": "List of ElecPriceDataRecord records"},
    )

    @classmethod
    @abstractmethod
    def provider_id(cls) -> str:
        return "ElecPriceProvider"

    def enabled(self) -> bool:
        return self.provider_id() == self.config.elecprice.provider

    # --- PricePredictionProviderBase hooks -------------------------------
    #
    # Concrete for every electricity-price data source: the raw/gross record
    # keys, the consumption-fee keys, and the fee formula itself don't vary
    # by provider (ElecPriceEnergyCharts, ElecPriceAkkudoktor, ...), only by
    # "electricity price" vs. "feed-in tariff" - so, unlike `provider_id`,
    # these are NOT left abstract for concrete providers to fill in.

    @property
    def _raw_key(self) -> str:
        """Record key holding the fee-free raw series."""
        return "elecprice_marketprice_raw_wh"

    @property
    def _gross_key(self) -> str:
        """Record key to write the fee-inclusive series to."""
        return "elecprice_marketprice_wh"

    @property
    def _fee_keys(self) -> list[str]:
        """Prediction keys to fetch for fee computation."""
        return ["elecfee_consumption_amt_wh", "elecfee_consumption_percent_amt"]

    def _compute_gross(self, raw_amt_wh: pd.Series, df_fee: pd.DataFrame) -> pd.Series:
        """Add the per-Wh consumption fee, then apply the percent surcharge (e.g. VAT).

        gross = (raw + elecfee_consumption_amt_wh) * (100 + elecfee_consumption_percent_amt) / 100

        Args:
            raw_amt_wh: Raw electricity market price (amount/Wh), fee-free.
            df_fee: Fee dataframe aligned to `raw_amt_wh`'s index, with columns
                matching `_fee_keys`.

        Returns:
            pd.Series: Gross electricity price (amount/Wh), same index as `raw_amt_wh`.
        """
        return (
            (raw_amt_wh + df_fee["elecfee_consumption_amt_wh"])
            * (100.0 + df_fee["elecfee_consumption_percent_amt"])
            / 100.0
        )

    # --- RL29 fork: ML forecast of the hours after the published prices ------

    _price_model: Optional[Any] = None

    def _ml_settings(self) -> Optional[Any]:
        settings = getattr(self.config.elecprice, "ml", None)
        return settings if settings is not None and settings.enabled else None

    def _ml_history_days(self) -> int:
        """Days of price history to keep and fetch (35 = what ETS needs)."""
        settings = self._ml_settings()
        return max(35, settings.history_days) if settings is not None else 35

    async def _weather_getters(self, start: Any, end: Any) -> dict[str, Callable[[float], float]]:
        """Weather at a Unix time from the prediction store (nearest record within 90 min)."""
        from akkudoktoreos.prediction.elecpriceml import WEATHER_KEYS

        getters: dict[str, Callable[[float], float]] = {}
        for key in WEATHER_KEYS:
            try:
                series = await self.prediction.key_to_raw_series(
                    key=key, start_datetime=start, end_datetime=end
                )
            except Exception as exc:
                logger.debug("Price ML: no weather series {}: {}", key, exc)
                continue
            if series is None or series.empty:
                continue
            stamps = np.array([pd.Timestamp(t).timestamp() for t in series.index], dtype=float)
            values = np.asarray(series.to_numpy(dtype=float), dtype=float)
            order = np.argsort(stamps)
            stamps, values = stamps[order], values[order]

            def getter(
                ts: float, stamps: np.ndarray = stamps, values: np.ndarray = values
            ) -> float:
                i = int(np.searchsorted(stamps, ts))
                best = math.nan
                gap = 5400.0
                for j in (i - 1, i):
                    if 0 <= j < len(stamps) and abs(stamps[j] - ts) <= gap:
                        gap = abs(stamps[j] - ts)
                        best = float(values[j])
                return best

            getters[key] = getter
        return getters

    async def _predict_tail(
        self,
        history: np.ndarray,
        count: int,
        slots_per_hour: int,
        resolution_seconds: int,
        last_datetime: Any,
    ) -> np.ndarray:
        """Prices after the last known slot: LightGBM when enabled and validated, else ETS."""

        def ets(values: np.ndarray, n: int) -> np.ndarray:
            return self._predict(values, n, slots_per_hour=slots_per_hour)

        settings = self._ml_settings()
        if settings is None:
            return ets(history, count)
        try:
            from zoneinfo import ZoneInfo

            from akkudoktoreos.prediction.elecpriceml import (
                PriceSeries,
                predict_prices,
                train_price_model,
            )
            from akkudoktoreos.utils.datetimeutil import to_datetime, to_duration

            tz = ZoneInfo(str(self.config.general.timezone or "UTC"))
            last_ts = pd.Timestamp(last_datetime).timestamp()
            slots = int(settings.history_days * 86400 / resolution_seconds)
            start_ts = last_ts - slots * resolution_seconds
            # Own read with an explicit start: index i is start + i * resolution and the
            # last element is the last published slot (the ETS input ends one earlier).
            prices = await self.key_to_array(
                key=self._raw_key,
                start_datetime=to_datetime(start_ts),
                end_datetime=to_datetime(last_ts + resolution_seconds),
                interval=to_duration(f"{resolution_seconds} seconds"),
                fill_method="linear",
            )
            prices = np.asarray(prices, dtype=float)[: slots + 1]
            start_ts = last_ts - (len(prices) - 1) * resolution_seconds
            # No made-up history before the first record (the resampling fills edges).
            raw = await self.key_to_raw_series(
                key=self._raw_key,
                start_datetime=to_datetime(start_ts),
                end_datetime=to_datetime(last_ts + resolution_seconds),
            )
            if raw is not None and not raw.empty:
                first_ts = pd.Timestamp(raw.index.min()).timestamp()
                skip = max(0, int(math.ceil((first_ts - start_ts) / resolution_seconds)))
                prices = prices[skip:]
                start_ts += skip * resolution_seconds
            weather = await self._weather_getters(
                to_datetime(start_ts),
                to_datetime(last_ts + (count + 1) * resolution_seconds),
            )
            series = PriceSeries(
                start=start_ts, step=int(resolution_seconds), price=prices, weather=weather
            )
            model = self._price_model
            stale = model is None or time.time() - model.trained_at > settings.refit_hours * 3600
            if stale:
                started = time.monotonic()
                model = train_price_model(series, settings, tz, ets)
                self._price_model = model
                self._write_price_report(model, time.monotonic() - started, len(weather))
                if model is not None:
                    logger.info(
                        "Price ML: trained on {} days in {:.1f} s, holdout MAE {} vs. ETS {} ct/kWh, {}.",
                        model.days,
                        time.monotonic() - started,
                        model.summary()["holdout_mae_ml_ct_kwh"],
                        model.summary()["holdout_mae_ets_ct_kwh"],
                        "used" if model.use else "not used (ETS is better)",
                    )
                else:
                    logger.info("Price ML: too little price history; ETS forecast.")
            if model is not None and model.use:
                return predict_prices(model, series, count, tz, settings.state)
        except ImportError as exc:
            logger.warning("Price ML: LightGBM unavailable ({}); ETS forecast.", exc)
        except Exception as exc:  # never lose the price forecast because of the model
            logger.warning("Price ML failed ({}); ETS forecast.", exc)
        return ets(history, count)

    def _write_price_report(self, model: Optional[Any], seconds: float, weather_keys: int) -> None:
        try:
            path = Path(self.config.general.data_folder_path) / "rl29" / "price_ml.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            data = (
                model.summary()
                if model is not None
                else {"used": False, "reason": "too little price history"}
            )
            data["train_seconds"] = round(seconds, 2)
            data["weather_keys"] = weather_keys
            path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.debug("Price ML: report not written: {}", exc)

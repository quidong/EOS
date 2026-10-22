"""Configuration-owned GENETIC requests with fresh runtime observations."""

import math
from pathlib import Path
from typing import Annotated, Any, Optional

from loguru import logger
from pydantic import AliasChoices, Field, field_validator

from akkudoktoreos.core.coreabc import (
    ConfigMixin,
    MeasurementMixin,
    PredictionMixin,
    get_ems,
)
from akkudoktoreos.devices.settings.batterysettings import BatteriesCommonSettings
from akkudoktoreos.optimization.genetic.forecast import bounded_forecast_array
from akkudoktoreos.optimization.genetic.geneticabc import GeneticParametersBaseModel
from akkudoktoreos.optimization.genetic.geneticparams import (
    GeneticEnergyManagementParameters,
    GeneticOptimizationParameters,
)
from akkudoktoreos.optimization.genetic.heatpumpgenes import GeneticHeatPumpParameters
from akkudoktoreos.utils.datetimeutil import DateTime, to_datetime, to_duration


class RuntimeForecasts(GeneticParametersBaseModel):
    """External series from local midnight, in slot Wh and currency per Wh.

    The interval is configured in optimization.genetic.interval_sec. Omitted
    series are read from the configured providers, preserving their raw coverage.
    """

    pv_forecast_wh: Optional[list[float]] = Field(
        default=None, validation_alias=AliasChoices("pv_forecast_wh", "pv_prognose_wh")
    )
    total_load: Optional[list[float]] = Field(
        default=None, validation_alias=AliasChoices("total_load", "gesamtlast")
    )
    electricity_price_per_wh: Optional[list[float]] = Field(
        default=None,
        validation_alias=AliasChoices("electricity_price_per_wh", "strompreis_euro_pro_wh"),
    )
    feed_in_tariff_per_wh: Optional[list[float]] = Field(
        default=None,
        validation_alias=AliasChoices("feed_in_tariff_per_wh", "einspeiseverguetung_euro_pro_wh"),
    )
    temperature_forecast: Optional[list[Optional[float]]] = None
    ghi_forecast: Optional[list[Optional[float]]] = None


class ConfigOptimizationRequest(
    ConfigMixin, MeasurementMixin, PredictionMixin, GeneticParametersBaseModel
):
    """Supply observations while hardware, tariffs and scheduling remain in config.

    Explicit SoC values take precedence over fresh measurements. Without a usable
    measurement, assume 0% initial SoC for the affected device and continue.
    """

    soc: dict[str, Annotated[int, Field(ge=0, le=100)]] = Field(default_factory=dict)
    forecasts: RuntimeForecasts = Field(default_factory=RuntimeForecasts)
    start_solution: Optional[list[float]] = None
    start_solution_datetime: Optional[DateTime] = None

    @field_validator("start_solution_datetime", mode="before")
    @classmethod
    def validate_start_solution_datetime(cls, value: Any) -> Optional[DateTime]:
        """Parse a previous-plan timestamp while retaining its explicit timezone."""
        return GeneticOptimizationParameters.transform_start_solution_datetime(value)

    async def resolve(self) -> GeneticOptimizationParameters:
        """Resolve inside the EMS lock after its slot start has been established."""
        config = self.config
        settings = config.optimization.genetic
        config.validate_optimization_horizons()
        ems = get_ems()
        start = ems.start_datetime
        observation_time = ems.observation_datetime
        devices = config.devices
        groups: dict[str, list[Any]] = {}
        for name in ("batteries", "electric_vehicles", "inverters", "home_appliances"):
            entries = list((getattr(devices, name) or {}).values())
            maximum = getattr(devices, "max_" + name)
            if maximum is not None and len(entries) > maximum:
                raise ValueError(f"devices.{name} exceeds configured maximum {maximum}.")
            if name != "home_appliances" and len(entries) > 1:
                raise ValueError(f"GENETIC supports at most one device in devices.{name}.")
            groups[name] = entries
        heat_pump_settings = list((devices.heat_pumps or {}).values())
        if devices.max_heat_pumps is not None and len(heat_pump_settings) > devices.max_heat_pumps:
            raise ValueError(
                f"devices.heat_pumps exceeds configured maximum {devices.max_heat_pumps}."
            )
        ids = [device.device_id for entries in groups.values() for device in entries]
        ids += [device.device_id for device in heat_pump_settings]
        if len(ids) != len(set(ids)):
            raise ValueError("Configured device IDs must be unique across device groups.")
        storage = groups["batteries"] + groups["electric_vehicles"]
        unknown = set(self.soc) - {device.device_id for device in storage}
        if unknown:
            raise ValueError(f"SoC supplied for unconfigured devices: {sorted(unknown)}.")

        async def state(device: BatteriesCommonSettings) -> int:
            if device.device_id in self.soc:
                return self.soc[device.device_id]
            try:
                dates, values = await self.measurement.key_to_lists(
                    key=device.measurement_key_soc_factor,
                    start_datetime=observation_time.subtract(
                        seconds=settings.measurement_max_age_seconds
                    ),
                    end_datetime=observation_time.add(seconds=1),
                    dropna=False,
                )
                samples = [
                    (date, value)
                    for date, value in zip(dates, values)
                    if date.timestamp() <= observation_time.timestamp()
                ]
                date, value = max(samples, key=lambda sample: sample[0].timestamp())
                if (
                    observation_time.timestamp() - date.timestamp()
                    > settings.measurement_max_age_seconds
                    or value is None
                    or not math.isfinite(value)
                    or not 0 <= value <= 1
                ):
                    raise ValueError("Invalid or stale SoC factor")
                return int(value * 100)
            except (ValueError, KeyError) as exc:
                logger.warning(
                    "No usable fresh SoC for {}; assuming 0% initial SoC for this "
                    "optimization (not a measurement): {}",
                    device.device_id,
                    exc,
                )
                return 0

        battery = None
        if groups["batteries"]:
            device = groups["batteries"][0]
            battery = device.to_genetic_pv_bat_param()
            battery.initial_soc_percentage = await state(device)
        ev = None
        if groups["electric_vehicles"]:
            device = groups["electric_vehicles"][0]
            ev = device.to_genetic_ev_bat_param()
            ev.initial_soc_percentage = await state(device)
        inverter = None
        if groups["inverters"]:
            device = groups["inverters"][0]
            if device.battery_id != (battery.device_id if battery else None):
                raise ValueError("Inverter battery_id must match the configured battery.")
            inverter = device.to_genetic_param()
        if inverter is None:
            raise ValueError("Configure an inverter to model PV and grid energy flows.")
        requested_devices = []
        for device in groups["home_appliances"]:
            resolved = await self._appliance_request(device, observation_time)
            if resolved is not None:
                requested_devices.append(resolved)
        groups["home_appliances"] = requested_devices
        appliances = [device.to_genetic_param() for device in groups["home_appliances"]]
        for device, appliance in zip(groups["home_appliances"], appliances):
            key = device.cycles_completed_measurement_key or f"{device.device_id}.cycles_completed"
            try:
                dates, counts = await self.measurement.key_to_lists(
                    key=key,
                    start_datetime=observation_time.start_of("day"),
                    end_datetime=observation_time.add(seconds=1),
                    dropna=False,
                )
            except KeyError:
                continue
            # RL29 fork: records written for other keys (e.g. the request
            # measurements) carry no count; only reported counts are considered.
            samples = [
                (date, count)
                for date, count in zip(dates, counts)
                if date.timestamp() <= observation_time.timestamp()
                and count is not None
                and not (isinstance(count, float) and math.isnan(count))
            ]
            if not samples:
                continue
            count = max(samples, key=lambda item: item[0].timestamp())[1]
            if (
                not math.isfinite(count)
                or int(count) != count
                or not 0 <= count <= appliance.num_cycles
            ):
                raise ValueError(f"Invalid completed cycle count for {device.device_id}.")
            appliance.completed_cycles = int(count)

        origin = start.start_of("day")
        if config.prediction.hours is None or config.prediction.hours <= 0:
            raise ValueError("Configure a positive prediction horizon.")
        end = start.add(hours=config.prediction.hours)
        interval = to_duration(settings.interval_sec)
        updated = False

        async def forecast(
            supplied: Optional[list[float]], key: str, energy: bool = False
        ) -> list[float]:
            nonlocal updated
            if supplied is not None:
                return list(supplied)
            if not updated:
                await self.prediction.update_data()
                updated = True
            values = await bounded_forecast_array(
                self.prediction, key=key, start_datetime=origin, end_datetime=end, interval=interval
            )
            if energy:
                values = values * (settings.interval_sec / 3600)
            return values.tolist()

        supplied = self.forecasts
        pv = await forecast(supplied.pv_forecast_wh, "pvforecast_ac_power", energy=True)
        load = await forecast(supplied.total_load, "loadforecast_power_w", energy=True)
        prices = await forecast(supplied.electricity_price_per_wh, "elecprice_marketprice_wh")
        tariffs = await forecast(supplied.feed_in_tariff_per_wh, "feed_in_tariff_wh")
        length = min(len(pv), len(load), len(prices), len(tariffs))
        if not length:
            raise ValueError("Forecast series must be nonempty.")
        pv, load, prices, tariffs = (values[:length] for values in (pv, load, prices, tariffs))
        first = int((start - origin).total_seconds() / settings.interval_sec)
        last = first + settings.horizon
        if settings.horizon <= 0 or len(pv) < last:
            raise ValueError("Forecast does not cover the positive GENETIC control horizon.")
        for key, values in (
            ("PV", pv),
            ("load", load),
            ("prices", prices),
            ("feed-in tariff", tariffs),
        ):
            if not all(math.isfinite(value) for value in values[first:last]):
                raise ValueError(f"Missing or invalid {key} within the control horizon.")
        if any(value < 0 for values in (pv, load) for value in values[first:last]):
            raise ValueError("PV and load energy must be nonnegative.")
        temperatures = await self._temperature_forecast(
            supplied.temperature_forecast, origin, end, interval, length
        )
        ghi = None
        heat_pumps = None
        if heat_pump_settings:
            ghi = await self._weather_forecast(
                supplied.ghi_forecast, "weather_ghi", origin, end, interval, length
            )
            heat_pumps = [
                await self._heat_pump_state(device, observation_time)
                for device in heat_pump_settings
            ]
        previous = ems.genetic_solution()
        warm_start = self.start_solution
        warm_start_time = self.start_solution_datetime
        if warm_start is None and previous is not None:
            warm_start = previous.start_solution
            warm_start_time = previous.start_solution_datetime
        return GeneticOptimizationParameters(
            forecast_interval_seconds=settings.interval_sec,
            ems=GeneticEnergyManagementParameters(
                pv_forecast_wh=pv,
                total_load=load,
                electricity_price_per_wh=prices,
                feed_in_tariff_per_wh=tariffs,
                price_per_wh_battery=settings.terminal_value_euro_per_kwh / 1000,
            ),
            pv_battery=battery,
            ev=ev,
            inverter=inverter,
            home_appliances=appliances,
            temperature_forecast=temperatures,
            ghi_forecast=ghi,
            heat_pumps=heat_pumps,
            start_solution=warm_start,
            start_solution_datetime=warm_start_time,
        )

    async def _temperature_forecast(
        self,
        supplied: Optional[list[Optional[float]]],
        origin: DateTime,
        end: DateTime,
        interval: Any,
        length: int,
    ) -> Optional[list[Optional[float]]]:
        """Outdoor temperature per slot in degC, aligned with the other forecasts.

        A supplied series takes precedence. Otherwise the configured weather
        provider's ``weather_temp_air`` is read from the prediction store (RL29
        fork: upstream left the forecast empty for configuration-owned runs, so
        temperature dependent device models had no input). Missing slots stay
        ``None``; no data at all yields ``None``. The temperature is optional, so
        a missing weather provider never cancels the optimization.
        """
        return await self._weather_forecast(
            supplied, "weather_temp_air", origin, end, interval, length
        )

    async def _weather_forecast(
        self,
        supplied: Optional[list[Optional[float]]],
        key: str,
        origin: DateTime,
        end: DateTime,
        interval: Any,
        length: int,
    ) -> Optional[list[Optional[float]]]:
        """Weather series per slot (see ``_temperature_forecast``)."""
        if supplied is not None:
            return (list(supplied[:length]) + [None] * length)[:length]
        try:
            values = await bounded_forecast_array(
                self.prediction,
                key=key,
                start_datetime=origin,
                end_datetime=end,
                interval=interval,
            )
        except (KeyError, ValueError) as exc:
            logger.debug("No {} forecast available: {}", key, exc)
            return None
        temperatures: list[Optional[float]] = [
            float(value) if math.isfinite(value) else None for value in values[:length]
        ]
        temperatures += [None] * (length - len(temperatures))
        if all(value is None for value in temperatures):
            return None
        return temperatures

    async def _appliance_request(self, device: Any, observation_time: DateTime) -> Optional[Any]:
        """Apply a pending request to a request-driven appliance (RL29 fork).

        Returns the settings to plan with, or ``None`` when the appliance has no
        fresh request and must not be planned. Appliances that are not
        request-driven are returned unchanged.
        """
        profile = None
        if getattr(device, "learn_profiles", False):
            profile = await self._learned_profile(device, observation_time)
        if not getattr(device, "request_driven", False):
            return device
        max_age = float(device.request_max_age_seconds)
        requested = await self._latest_measurement(
            device.measurement_key_requested, observation_time, max_age
        )
        if requested is None or requested < 0.5:
            logger.debug("Appliance {}: no pending request, not planned.", device.device_id)
            return None
        update: dict[str, Any] = {}
        deadline = await self._latest_measurement(
            device.measurement_key_deadline, observation_time, max_age
        )
        if deadline is not None and deadline > 0:
            update["deadline_datetime"] = to_datetime(
                deadline, in_timezone=self.config.general.timezone
            )
            if device.earliest_start_datetime is not None and not (
                update["deadline_datetime"] > device.earliest_start_datetime
            ):
                update["earliest_start_datetime"] = None
        duration = await self._latest_measurement(
            device.measurement_key_duration, observation_time, max_age
        )
        if profile is not None:
            update["load_profile_power_w"] = list(profile["power_w"])
            update["load_profile_interval_seconds"] = int(profile["interval_seconds"])
            update["consumption_wh"] = None
            update["duration_h"] = None
        elif duration is not None and duration > 0 and device.load_profile_power_w is None:
            hours = min(24, max(1, math.ceil(duration - 1e-6)))
            if device.duration_h and device.consumption_wh:
                # Keep the average power of the configured run.
                update["consumption_wh"] = max(
                    1, round(device.consumption_wh * hours / device.duration_h)
                )
            update["duration_h"] = hours
        if profile is not None:
            logger.info(
                "Appliance {}: learned profile of program {} ({} runs, {} Wh, {} min).",
                device.device_id,
                profile.get("program"),
                profile.get("runs"),
                profile.get("energy_wh"),
                round((profile.get("duration_s") or 0) / 60),
            )
        logger.info(
            "Appliance {}: request pending, deadline {}, duration {} h.",
            device.device_id,
            update.get("deadline_datetime", device.deadline_datetime),
            update.get("duration_h", device.duration_h),
        )
        if not update:
            return device
        data = device.model_dump(exclude=set(type(device).model_computed_fields))
        data.update(update)
        return type(device).model_validate(data)

    async def _learned_profile(self, device: Any, observation_time: DateTime) -> Optional[dict]:
        """Learn finished runs and return the profile of the selected program (RL29 fork)."""
        from akkudoktoreos.devices.applianceprofiles import (
            ApplianceProfileStore,
            detect_runs,
        )

        path = Path(self.config.general.data_folder_path) / "rl29" / "appliance_profiles.json"
        store = ApplianceProfileStore(path)
        since = observation_time.subtract(days=2)

        async def series(key: str) -> list[tuple[Any, Optional[float]]]:
            try:
                dates, values = await self.measurement.key_to_lists(
                    key=key,
                    start_datetime=since,
                    end_datetime=observation_time.add(seconds=1),
                    dropna=True,
                )
            except (KeyError, ValueError):
                return []
            return list(zip(dates, values))

        try:
            runs = detect_runs(
                await series(device.measurement_key_power),
                await series(device.measurement_key_program),
                now=observation_time,
                threshold_w=device.profile_power_threshold_w,
                window_start=since,
            )
            outcome = store.learn(
                device.device_id,
                runs,
                device.profile_learning_rate,
                duration_quantile=device.profile_duration_quantile,
                outlier_tolerance=device.profile_outlier_tolerance,
            )
            if outcome:
                store.save()
            for rejected in outcome.rejected_runs:
                logger.warning(
                    "Appliance {}: run {} rejected as outlier, profile unchanged.",
                    device.device_id,
                    rejected,
                )
            for program_code in outcome.restarted:
                logger.warning(
                    "Appliance {}: program {} deviated repeatedly; its profile starts over.",
                    device.device_id,
                    program_code,
                )
        except (OSError, ValueError) as exc:
            logger.warning("Appliance {}: profile learning failed: {}", device.device_id, exc)
        program = await self._latest_measurement(
            device.measurement_key_program, observation_time, 24 * 3600
        )
        if program is None:
            return None
        code = str(int(program)) if float(program).is_integer() else str(program)
        entry = store.profile(device.device_id, code)
        if entry is None:
            return None
        return {**entry, "program": code}

    async def _latest_measurement(
        self, key: str, observation_time: DateTime, max_age_seconds: float
    ) -> Optional[float]:
        """Latest finite value of a measurement key not older than ``max_age_seconds``."""
        try:
            dates, values = await self.measurement.key_to_lists(
                key=key,
                start_datetime=observation_time.subtract(seconds=max_age_seconds),
                end_datetime=observation_time.add(seconds=1),
                dropna=True,
            )
        except (KeyError, ValueError):
            return None
        samples = [
            (date, value)
            for date, value in zip(dates, values)
            if date.timestamp() <= observation_time.timestamp()
            and value is not None
            and math.isfinite(value)
        ]
        if not samples:
            return None
        return float(max(samples, key=lambda sample: sample[0].timestamp())[1])

    async def _heat_pump_state(
        self, device: Any, observation_time: DateTime
    ) -> GeneticHeatPumpParameters:
        """Current thermal state of a heat pump from fresh measurements (RL29 fork).

        Missing or stale values stay ``None`` and the optimizer uses model
        defaults. The time since the last legionella heat-up is read from the
        hot water temperature history.
        """
        max_age = self.config.optimization.genetic.measurement_max_age_seconds
        room = await self._latest_measurement(
            device.measurement_key_room_temperature, observation_time, max_age
        )
        slab = await self._latest_measurement(
            device.measurement_key_slab_temperature, observation_time, max_age
        )
        dhw = await self._latest_measurement(
            device.measurement_key_dhw_temperature, observation_time, max_age
        )
        setpoint = await self._latest_measurement(
            device.measurement_key_dhw_setpoint, observation_time, 7 * 24 * 3600
        )
        if setpoint is not None and not 30.0 <= setpoint <= 65.0:
            logger.warning(
                "Heat pump {}: ignoring hot water setpoint {}.", device.device_id, setpoint
            )
            setpoint = None
        hours_since: Optional[float] = None
        interval_days = device.dhw.legionella_interval_days
        if interval_days is not None:
            try:
                dates, values = await self.measurement.key_to_lists(
                    key=device.measurement_key_dhw_temperature,
                    start_datetime=observation_time.subtract(days=interval_days + 1),
                    end_datetime=observation_time.add(seconds=1),
                    dropna=True,
                )
            except (KeyError, ValueError):
                dates, values = [], []
            threshold = device.dhw.legionella_c - 0.5
            hot = [
                date
                for date, value in zip(dates, values)
                if value is not None
                and math.isfinite(value)
                and value >= threshold
                and date.timestamp() <= observation_time.timestamp()
            ]
            if hot:
                last = max(hot, key=lambda date: date.timestamp())
                hours_since = (observation_time.timestamp() - last.timestamp()) / 3600.0
            elif dates:
                # History exists but never reached the temperature: overdue.
                first = min(dates, key=lambda date: date.timestamp())
                covered_h = (observation_time.timestamp() - first.timestamp()) / 3600.0
                if covered_h >= interval_days * 24.0 * 0.9:
                    hours_since = interval_days * 24.0
        missing = [name for name, value in (("room", room), ("dhw", dhw)) if value is None]
        if missing:
            logger.warning(
                "Heat pump {}: no measurement younger than {} s for {}; using model defaults.",
                device.device_id,
                max_age,
                ", ".join(missing),
            )
        logger.debug(
            "Heat pump {} state: room {} slab {} dhw {} setpoint {} legionella {} h ago",
            device.device_id,
            room,
            slab,
            dhw,
            setpoint,
            hours_since,
        )
        # §14a EnWG: dimming signal and module 2 (own meter, reduced price).
        grid_14a = self.config.elecfee.grid_control_14a
        power_limited_w: Optional[float] = None
        if grid_14a.is_controllable(device.device_id):
            limited = await self._latest_measurement(
                device.measurement_key_power_limited, observation_time, max_age
            )
            if limited is not None and limited > 0.5:
                power_limited_w = grid_14a.dimming_power_w
                logger.info(
                    "Heat pump {}: §14a dimming active, limit {} W.",
                    device.device_id,
                    power_limited_w,
                )
        reduction_wh: Optional[float] = None
        if grid_14a.separately_metered(device.device_id):
            vat = self.config.elecfee.elecfeefixed.consumption_percent_amt.get_value_for_datetime(
                observation_time
            )
            reduction_wh = grid_14a.module2_reduction_amt_kwh() * (100.0 + vat) / 100.0 / 1000.0
        cooling_season = False
        dewpoint: Optional[float] = None
        if device.cooling.enabled:
            season = await self._latest_measurement(
                device.measurement_key_cooling_season, observation_time, 6 * 3600
            )
            cooling_season = season is not None and season >= 0.5
            dewpoint = await self._latest_measurement(
                device.measurement_key_dewpoint, observation_time, max_age
            )
            if cooling_season:
                logger.info(
                    "Heat pump {}: cooling season, dew point {} degC.", device.device_id, dewpoint
                )
        return GeneticHeatPumpParameters(
            device_id=device.device_id,
            cooling_season=cooling_season,
            dewpoint_c=dewpoint,
            settings=device,
            initial_room_c=room,
            initial_slab_c=slab,
            initial_dhw_c=dhw,
            dhw_setpoint_c=setpoint,
            power_limited_w=power_limited_w,
            power_limited_hours=grid_14a.dimming_hold_hours,
            separate_meter_price_reduction_amt_wh=reduction_wh,
            hours_since_legionella=hours_since,
        )

"""Open-Meteo outages must not cancel the optimization (RL29).

On 2026-11-02 Open-Meteo answered ``503 Service Unavailable`` ("The service is
overloaded") for hours. The weather provider raised, the prediction container
re-raised, and every GENETIC run was cancelled although the store still held a
forecast for the next days. These tests pin the fix: retry transient errors and
keep the previous forecast when one still reaches past the run start.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests

from akkudoktoreos.core.cache import CacheFileStore
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.prediction.weatheropenmeteo import WeatherOpenMeteo
from akkudoktoreos.utils.datetimeutil import to_datetime

TESTDATA = Path(__file__).absolute().parent.parent / "testdata" / "weatherforecast_openmeteo_1.json"
RUN_START = "2026-03-02 09:00:00+01:00"


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("EOS_WEATHER__WEATHER_PROVIDER", "OpenMeteo")
    monkeypatch.setenv("EOS_GENERAL__LATITUDE", "50.0")
    monkeypatch.setenv("EOS_GENERAL__LONGITUDE", "10.0")
    CacheFileStore().clear(clear_all=True)
    get_ems().set_start_datetime(to_datetime(RUN_START, in_timezone="Europe/Berlin"))
    return WeatherOpenMeteo()


@pytest.fixture
def sample() -> dict[str, Any]:
    with TESTDATA.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _ok(sample: dict[str, Any]) -> Mock:
    response = Mock()
    response.status_code = 200
    response.json.return_value = sample
    response.content = json.dumps(sample)
    response.raise_for_status.return_value = None
    return response


def _failing(status: int) -> Mock:
    response = Mock()
    response.status_code = status
    response.raise_for_status.side_effect = requests.HTTPError(
        f"{status} Server Error", response=response
    )
    return response


async def _load_forecast(provider: WeatherOpenMeteo, sample: dict[str, Any]) -> None:
    with patch("requests.get", return_value=_ok(sample)):
        await provider.update_data(force_enable=True, force_update=True)
    CacheFileStore().clear(clear_all=True)


@pytest.mark.asyncio
async def test_transient_503_is_retried(provider, sample):
    with (
        patch("requests.get", side_effect=[_failing(503), _ok(sample)]) as get,
        patch("akkudoktoreos.prediction.weatheropenmeteo.time.sleep") as sleep,
    ):
        await provider.update_data(force_enable=True, force_update=True)

    assert get.call_count == 2
    sleep.assert_called_once()
    assert len(provider) > 0


@pytest.mark.asyncio
async def test_client_error_is_not_retried(provider):
    await provider.delete_by_datetime()
    with (
        patch("requests.get", return_value=_failing(400)) as get,
        patch("akkudoktoreos.prediction.weatheropenmeteo.time.sleep") as sleep,
    ):
        with pytest.raises(requests.HTTPError):
            await provider.update_data(force_enable=True, force_update=True)

    assert get.call_count == 1
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_outage_keeps_the_previous_forecast(provider, sample):
    """Reproduces 2026-11-02: three 503 answers must not raise once data exists."""
    await _load_forecast(provider, sample)
    stored_until = await provider.max_datetime()
    records_before = len(provider)
    assert records_before > 0

    with (
        patch("requests.get", return_value=_failing(503)) as get,
        patch("akkudoktoreos.prediction.weatheropenmeteo.time.sleep"),
    ):
        await provider.update_data(force_enable=True, force_update=True)

    assert get.call_count == 3
    assert await provider.max_datetime() == stored_until
    assert len(provider) == records_before
    dates, values = await provider.key_to_lists(
        "weather_temp_air", start_datetime=get_ems().start_datetime
    )
    assert any(value is not None for value in values)


@pytest.mark.asyncio
async def test_outage_without_any_forecast_still_fails(provider):
    """A cold start has nothing to fall back to, so the caller must hear about it."""
    await provider.delete_by_datetime()
    with (
        patch("requests.get", return_value=_failing(503)),
        patch("akkudoktoreos.prediction.weatheropenmeteo.time.sleep"),
    ):
        with pytest.raises(requests.HTTPError):
            await provider.update_data(force_enable=True, force_update=True)


@pytest.mark.asyncio
async def test_outage_after_the_forecast_ended_still_fails(provider, sample):
    """Data that lies completely before the run start is no usable fallback."""
    await _load_forecast(provider, sample)
    stored_until = await provider.max_datetime()
    get_ems().set_start_datetime(stored_until.add(days=1))

    with (
        patch("requests.get", return_value=_failing(503)),
        patch("akkudoktoreos.prediction.weatheropenmeteo.time.sleep"),
    ):
        with pytest.raises(requests.HTTPError):
            await provider.update_data(force_enable=True, force_update=True)

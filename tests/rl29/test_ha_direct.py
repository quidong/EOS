"""Direct Home Assistant connection without the Supervisor bridge (RL29 fork)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from akkudoktoreos.adapter import homeassistant as ha
from akkudoktoreos.adapter.homeassistant import HomeAssistantAdapter, homeassistant_api
from akkudoktoreos.prediction.pvforecasthomeassistant import PVForecastHomeAssistant


class TestHomeassistantApi:
    def test_direct_url_and_token(self):
        api, headers = homeassistant_api("http://192.168.30.180:8123", "abc")
        assert api == "http://192.168.30.180:8123/api"
        assert headers["Authorization"] == "Bearer abc"

    @pytest.mark.parametrize(
        "url", ["http://ha:8123/", "http://ha:8123/api", "http://ha:8123/api/"]
    )
    def test_url_normalisation(self, url):
        api, _ = homeassistant_api(url, "t")
        assert api == "http://ha:8123/api"

    def test_direct_url_requires_token(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor")
        with pytest.raises(RuntimeError, match="token"):
            homeassistant_api("http://ha:8123", None)

    def test_supervisor_fallback(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor")
        api, headers = homeassistant_api(None, None)
        assert api == ha.CORE_API
        assert headers["Authorization"] == "Bearer supervisor"

    def test_supervisor_without_token_fails(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="base_url"):
            homeassistant_api(None, None)


@pytest.fixture
def adapter(config_eos, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    config_eos.merge_settings_from_dict(
        {
            "adapter": {
                "provider": ["HomeAssistant"],
                "homeassistant": {"base_url": "http://192.168.30.180:8123", "token": "llat"},
            }
        }
    )
    return HomeAssistantAdapter()


def _response(ok=True, status=200, payload=None):
    resp = MagicMock(ok=ok, status_code=status, text="")
    resp.json.return_value = payload
    return resp


def test_adapter_reads_state_directly(adapter, monkeypatch):
    get = MagicMock(return_value=_response(payload={"state": "21.5"}))
    monkeypatch.setattr(ha.requests, "get", get)
    assert adapter.get_entity_state("sensor.x") == "21.5"
    url = get.call_args.args[0]
    assert url == "http://192.168.30.180:8123/api/states/sensor.x"
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer llat"


def test_adapter_writes_state_directly(adapter, monkeypatch):
    post = MagicMock(return_value=_response(status=201))
    monkeypatch.setattr(ha.requests, "post", post)
    adapter.set_entity_state("sensor.eos_test", "1", {"unit_of_measurement": "W"})
    assert post.call_args.args[0] == "http://192.168.30.180:8123/api/states/sensor.eos_test"
    assert post.call_args.kwargs["json"] == {"state": "1", "attributes": {"unit_of_measurement": "W"}}


def test_adapter_lists_entities_directly(adapter, monkeypatch):
    payload = [{"entity_id": "sensor.a"}, {"entity_id": "sensor.eos_plan"}, {"entity_id": "light.b"}]
    get = MagicMock(return_value=_response(payload=payload))
    monkeypatch.setattr(ha.requests, "get", get)
    assert adapter.get_homeassistant_entity_ids() == ["light.b", "sensor.a"]
    assert get.call_args.args[0] == "http://192.168.30.180:8123/api/states"


def test_token_from_environment(config_eos, monkeypatch):
    """The token can come from EOS_ADAPTER__HOMEASSISTANT__TOKEN instead of the config file."""
    monkeypatch.setenv("EOS_ADAPTER__HOMEASSISTANT__TOKEN", "from-env")
    monkeypatch.setenv("EOS_ADAPTER__HOMEASSISTANT__BASE_URL", "http://ha:8123")
    config_eos.reset_settings()
    assert config_eos.adapter.homeassistant.token == "from-env"
    assert config_eos.adapter.homeassistant.base_url == "http://ha:8123"


def test_pv_provider_reuses_adapter_connection(config_eos, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    config_eos.merge_settings_from_dict(
        {
            "adapter": {"homeassistant": {"base_url": "http://ha:8123", "token": "shared"}},
            "pvforecast": {
                "provider": "PVForecastHomeAssistant",
                "homeassistant": {"entity_id": "sensor.helios_forecast"},
            },
        }
    )
    PVForecastHomeAssistant.reset_instance()
    provider = PVForecastHomeAssistant()
    assert provider._api_base_and_token() == ("http://ha:8123/api", "shared")


def test_pv_provider_own_settings_win(config_eos, monkeypatch):
    config_eos.merge_settings_from_dict(
        {
            "adapter": {"homeassistant": {"base_url": "http://ha:8123", "token": "shared"}},
            "pvforecast": {
                "provider": "PVForecastHomeAssistant",
                "homeassistant": {
                    "entity_id": "sensor.helios_forecast",
                    "base_url": "http://other:8123",
                    "token": "own",
                },
            },
        }
    )
    PVForecastHomeAssistant.reset_instance()
    provider = PVForecastHomeAssistant()
    assert provider._api_base_and_token() == ("http://other:8123/api", "own")

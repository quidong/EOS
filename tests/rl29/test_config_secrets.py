"""Secrets never leave the REST API (RL29 fork, server/rest/secrets.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akkudoktoreos.server.rest.secrets import (
    MASK,
    mask_value,
    path_is_secret,
    redact,
    strip_masked,
)

TOKEN = "eyJhbGciOiJIUzI1NiJ9.secret-token-value"
DSN = "postgresql://eos:VerySecret123@eos-history:5432/eos"


def test_pure_helpers():
    assert path_is_secret("adapter/homeassistant/token")
    assert path_is_secret("database/history/dsn")
    assert path_is_secret("elecprice/tibber/access_token")
    assert not path_is_secret("load/loadml/temperature_key")
    assert not path_is_secret("adapter/homeassistant/device_measurement_entity_ids")
    assert mask_value("dsn", DSN) == f"postgresql://eos:{MASK}@eos-history:5432/eos"
    assert (
        mask_value("dsn", "host=db password=abc dbname=eos")
        == f"host=db password={MASK} dbname=eos"
    )
    assert mask_value("token", None) is None and mask_value("token", "") == ""
    data = {"a": {"token": TOKEN, "temperature_key": "x", "list": [{"api_key": "k"}]}, "dsn": DSN}
    red = redact(data)
    assert red["a"]["token"] == MASK and red["a"]["temperature_key"] == "x"
    assert red["a"]["list"][0]["api_key"] == MASK
    assert "VerySecret" not in str(red)
    assert data["a"]["token"] == TOKEN  # the input is untouched
    assert strip_masked(red) == {"a": {"temperature_key": "x", "list": [{}]}}


@pytest.fixture
def client(config_eos):
    from akkudoktoreos.server.eos import app

    config_eos.merge_settings_from_dict(
        {
            "adapter": {"homeassistant": {"token": TOKEN}},
            "database": {"history": {"dsn": DSN}},
        }
    )
    return TestClient(app), config_eos


def test_config_responses_are_redacted(client):
    http, config = client
    for response in (
        http.get("/v1/config"),
        http.get("/v1/config/adapter"),
        http.get("/v1/config/adapter/homeassistant"),
        http.put("/v1/config/ems/interval", json=120),
    ):
        assert response.status_code == 200, response.text
        assert TOKEN not in response.text
        assert "VerySecret123" not in response.text
    assert http.get("/v1/config/adapter/homeassistant/token").json() == MASK
    assert http.get("/v1/config/database/history/dsn").json().endswith("@eos-history:5432/eos")
    # The value itself is unchanged inside EOS
    assert config.adapter.homeassistant.token == TOKEN
    assert config.ems.interval == 120


def test_masked_values_written_back_keep_the_secret(client):
    http, config = client
    exported = http.get("/v1/config").json()
    assert http.put("/v1/config/adapter/homeassistant/token", json=MASK).status_code == 200
    assert config.adapter.homeassistant.token == TOKEN
    masked_dsn = http.get("/v1/config/database/history/dsn").json()
    assert http.put("/v1/config/database/history/dsn", json=masked_dsn).status_code == 200
    assert config.database.history.dsn == DSN
    # Full import of an exported (masked) configuration
    exported["ems"]["interval"] = 90
    response = http.put("/v1/config", json=exported)
    assert response.status_code == 200, response.text
    assert config.adapter.homeassistant.token == TOKEN
    assert config.database.history.dsn == DSN
    assert config.ems.interval == 90
    # A real new value is accepted
    assert http.put("/v1/config/adapter/homeassistant/token", json="new-token").status_code == 200
    assert config.adapter.homeassistant.token == "new-token"

"""Tests for the REST API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataclasses import replace

from wolf.api import create_app
from wolf.app import build_application


@pytest.fixture
def client(settings):
    app_obj = build_application(settings)
    api = create_app(app_obj)
    return TestClient(api), app_obj


@pytest.fixture
def secured_client(settings):
    app_obj = build_application(replace(settings, api_key="secret123"))
    api = create_app(app_obj)
    return TestClient(api)


def test_health(client):
    api, _ = client
    resp = api.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["telegram_enabled"] is False  # no creds in test settings


def test_health_redacts_secrets(client):
    api, _ = client
    body = api.get("/health").json()
    # Secret fields are reported only as booleans, never their values.
    assert body["config"]["gemini_api_key"] in (True, False)


def test_record_and_list_active(client):
    api, _ = client
    payload = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "entry_price": 100,
        "tp": 110,
        "sl": 95,
        "strategy": "MANUAL",
    }
    resp = api.post("/signals", json=payload)
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True

    active = api.get("/signals/active").json()
    assert active["count"] == 1
    assert active["signals"][0]["symbol"] == "BTCUSDT"


def test_record_duplicate_rejected(client):
    api, _ = client
    payload = {"symbol": "ETHUSDT", "direction": "LONG", "entry_price": 100, "tp": 110, "sl": 95}
    assert api.post("/signals", json=payload).json()["recorded"] is True
    assert api.post("/signals", json=payload).json()["recorded"] is False


def test_stats_empty(client):
    api, _ = client
    stats = api.get("/stats").json()
    assert stats["total_resolved"] == 0
    assert stats["win_rate"] == 0.0


# ── API key guard ──────────────────────────────────────────────────────────
def test_mutating_endpoint_requires_key_when_configured(secured_client):
    payload = {"symbol": "BTCUSDT", "direction": "LONG", "entry_price": 100, "tp": 110, "sl": 95}
    assert secured_client.post("/signals", json=payload).status_code == 401
    ok = secured_client.post("/signals", json=payload, headers={"X-API-Key": "secret123"})
    assert ok.status_code == 200


def test_read_endpoints_open_even_when_key_configured(secured_client):
    assert secured_client.get("/health").status_code == 200
    assert secured_client.get("/stats").status_code == 200


def test_open_when_no_key_configured(client):
    api, _ = client
    payload = {"symbol": "ETHUSDT", "direction": "LONG", "entry_price": 100, "tp": 110, "sl": 95}
    assert api.post("/signals", json=payload).status_code == 200

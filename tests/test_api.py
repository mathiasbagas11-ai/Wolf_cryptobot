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


class _FakeFlow:
    def build(self):
        return "FLOW REPORT TEXT"
    def build_token(self, symbol):
        return f"DEEP DIVE {symbol.upper()}" if symbol.upper() == "ENA" else None


def test_flow_endpoint_posts_report(client):
    api, app_obj = client
    app_obj.flow = _FakeFlow()
    resp = api.post("/flow")
    assert resp.status_code == 200
    assert resp.json()["text"] == "FLOW REPORT TEXT"


def test_flow_deep_dive_endpoint(client):
    """The deep dive is its own on-demand reporter, separate from the digest."""
    api, app_obj = client
    app_obj.deepdive = _FakeFlow()
    assert api.post("/flow/ena").json()["text"] == "DEEP DIVE ENA"
    assert api.post("/flow/nope").status_code == 404


def test_flow_endpoint_503_when_reporting_is_disabled(client):
    api, app_obj = client
    app_obj.flow = None
    assert api.post("/flow").status_code == 503


class _FakeRanker:
    def __init__(self, card="RANKING CARD"):
        self.card = card
        self.forced = None

    def build(self, force=False):
        self.forced = force
        return self.card


def test_rank_endpoint_posts_the_ranking(client):
    api, app_obj = client
    ranker = _FakeRanker()
    app_obj.conviction = ranker
    resp = api.post("/rank")
    assert resp.status_code == 200
    assert resp.json()["text"] == "RANKING CARD"
    # An explicit request is answered even when the ranking has not changed.
    assert ranker.forced is True


def test_rank_endpoint_503_when_there_is_nothing_to_rank(client):
    api, app_obj = client
    app_obj.conviction = _FakeRanker(card=None)
    assert api.post("/rank").status_code == 503


def test_rank_endpoint_503_when_ranking_is_disabled(client):
    api, app_obj = client
    app_obj.conviction = None
    assert api.post("/rank").status_code == 503


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


def test_reads_need_the_key_once_one_is_configured(secured_client):
    """The ledger and paper account are the owner's, and the /whatif replays
    cost one klines request per signal: an open GET is a leak and a way to
    burn the bot's rate limit."""
    assert secured_client.get("/health").status_code == 200
    for path in ("/stats", "/paper", "/signals/active", "/signals/outcomes",
                 "/diagnostics", "/montecarlo"):
        assert secured_client.get(path).status_code == 401, path
        assert secured_client.get(path, headers={"X-API-Key": "secret123"}).status_code == 200, path


def test_health_tells_a_stranger_only_that_the_bot_is_alive(secured_client):
    body = secured_client.get("/health").json()
    assert body == {"status": "ok", "api_key_set": True}
    full = secured_client.get("/health", headers={"X-API-Key": "secret123"}).json()
    assert "config" in full and "state_dir" in full


def test_a_public_domain_with_no_key_refuses_instead_of_serving_everyone(settings):
    app_obj = build_application(replace(settings, public_domain="wolf.up.railway.app"))
    api = TestClient(create_app(app_obj))
    resp = api.get("/paper")
    assert resp.status_code == 503 and "API_KEY" in resp.json()["detail"]
    assert api.post("/scan").status_code == 503
    assert api.get("/health").json() == {"status": "ok", "api_key_set": False}


def test_paper_carries_the_net_ledger_beside_the_gross_balance(secured_client):
    body = secured_client.get("/paper", headers={"X-API-Key": "secret123"}).json()
    assert "ledger" in body and "balance" in body


def test_montecarlo_is_served_as_json_and_as_the_card(secured_client):
    h = {"X-API-Key": "secret123"}
    assert "trades" in secured_client.get("/montecarlo", headers=h).json()
    assert secured_client.get("/montecarlo?format=text", headers=h).text.startswith("MONTE-CARLO")


def test_open_when_no_key_configured(client):
    api, _ = client
    payload = {"symbol": "ETHUSDT", "direction": "LONG", "entry_price": 100, "tp": 110, "sl": 95}
    assert api.post("/signals", json=payload).status_code == 200


# ── outcome import (restore after an ephemeral state dir was wiped) ─────────
def test_health_reports_state_location_and_depth(client):
    api, _ = client
    body = api.get("/health").json()
    assert body["state_dir"].startswith("/")   # absolute: verifiable against a volume
    assert body["outcomes_stored"] == 0


def test_import_outcomes_restores_exported_log(client):
    api, app_obj = client
    export = {"count": 2, "outcomes": [
        {"id": "BTCUSDT_1", "symbol": "BTCUSDT", "signal_type": "SCREENER",
         "direction": "LONG", "entry_price": 100.0, "tp": 110.0, "sl": 95.0,
         "status": "TP_HIT", "pnl_pct": 10.0, "strategy": "SCALP"},
        {"id": "ETHUSDT_2", "symbol": "ETHUSDT", "signal_type": "SCREENER",
         "direction": "LONG", "entry_price": 100.0, "tp": 110.0, "sl": 95.0,
         "status": "SL_HIT", "pnl_pct": -5.0, "strategy": "SCALP"},
    ]}
    resp = api.post("/signals/outcomes/import", json=export)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 2
    assert len(app_obj.tracker.outcomes()) == 2
    assert app_obj.tracker.stats()["wins"] == 1


def test_import_outcomes_is_idempotent(client):
    """Re-running an import must not duplicate the sample it restores."""
    api, app_obj = client
    export = {"outcomes": [
        {"id": "BTCUSDT_1", "symbol": "BTCUSDT", "signal_type": "SCREENER",
         "direction": "LONG", "entry_price": 100.0, "tp": 110.0, "sl": 95.0,
         "status": "TP_HIT", "pnl_pct": 10.0, "strategy": "SCALP"},
    ]}
    api.post("/signals/outcomes/import", json=export)
    second = api.post("/signals/outcomes/import", json=export)
    assert second.json() == {"imported": 0, "skipped_without_id": 0, "total_stored": 1}
    assert len(app_obj.tracker.outcomes()) == 1


def test_import_outcomes_accepts_bare_list_and_rejects_junk(client):
    api, _ = client
    ok = api.post("/signals/outcomes/import", json=[
        {"id": "X_1", "symbol": "X", "signal_type": "SCREENER", "direction": "LONG",
         "entry_price": 1.0, "tp": 2.0, "sl": 0.5, "status": "TP_HIT"},
    ])
    assert ok.json()["imported"] == 1
    assert api.post("/signals/outcomes/import", json={"outcomes": "nope"}).status_code == 400


def test_diagnostics_json_and_text(client):
    api, _ = client
    body = api.get("/diagnostics").json()
    assert body["overall"]["verdict"] == "INCONCLUSIVE"  # empty history buys nothing
    assert "cost" in body and "concurrency" in body

    text = api.get("/diagnostics", params={"format": "text"}).text
    assert text.startswith("WOLF-DIAG v1")


def test_health_reports_ai_intent_versus_reality(client):
    """enabled vs available: when they disagree, every signal abstains."""
    api, _ = client
    ai = api.get("/health").json()["ai"]
    assert ai["enabled"] is False          # no AI configured in test settings
    assert ai["available"] is False


def test_the_api_listens_where_railway_routes_a_generated_domain(monkeypatch):
    from wolf.config import Settings

    monkeypatch.delenv("API_PORT", raising=False)
    monkeypatch.setenv("PORT", "8080")
    assert Settings.from_env().api_port == 8080
    monkeypatch.setenv("API_PORT", "9000")
    assert Settings.from_env().api_port == 9000

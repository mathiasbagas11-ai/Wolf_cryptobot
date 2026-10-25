"""Phase 2 verification — OHLC fetch + volume merge (network-free)."""

from __future__ import annotations

import pandas as pd

from anomaly.ohlc import fetch_ohlc, merge_ohlc_volume

DAY_MS = 86_400_000
START = 1_700_000_000_000


def _ohlc_payload(n=90):
    # [ms, open, high, low, close] daily candles with a gentle uptrend.
    return [[START + i * DAY_MS, 100 + i, 105 + i, 95 + i, 102 + i] for i in range(n)]


def _volume_payload(n=90):
    # market_chart volumes on a slightly offset cadence (hourly-ish stamps).
    return {"total_volumes": [[START + i * DAY_MS + 3600_000, 1_000_000 + i * 1000]
                              for i in range(n)]}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("http error")

    def json(self):
        return self._payload


class _FakeSession:
    """Returns the OHLC payload for /ohlc and the volume payload for /market_chart."""

    def __init__(self, ohlc, chart):
        self._ohlc = ohlc
        self._chart = chart
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        if url.endswith("/ohlc"):
            return _Resp(self._ohlc)
        return _Resp(self._chart)


def test_merge_produces_full_ohlcv_frame():
    df = merge_ohlc_volume(_ohlc_payload(90), _volume_payload(90))
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 90
    assert not df["close"].isna().any()
    assert (df["volume"] > 0).all()
    assert str(df["timestamp"].dt.tz) == "UTC"


def test_merge_handles_missing_volume_series():
    df = merge_ohlc_volume(_ohlc_payload(70), {})
    assert len(df) == 70
    assert (df["volume"] == 0.0).all()
    assert not df["close"].isna().any()


def test_merge_empty_ohlc_returns_empty_frame():
    df = merge_ohlc_volume([], _volume_payload(10))
    assert df.empty
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_fetch_ohlc_verify_contract(tmp_path):
    """The Phase-2 VERIFY: non-empty, no NaN close, >= 60 rows."""
    sess = _FakeSession(_ohlc_payload(90), _volume_payload(90))
    df = fetch_ohlc("bitcoin", days=90, session=sess,
                    cache_dir=str(tmp_path), sleep=0)
    assert not df.empty
    assert not df["close"].isna().any()
    assert len(df) >= 60
    # Both endpoints were hit.
    assert any(u.endswith("/ohlc") for u in sess.calls)
    assert any(u.endswith("/market_chart") for u in sess.calls)


def test_fetch_ohlc_uses_parquet_cache(tmp_path):
    sess = _FakeSession(_ohlc_payload(90), _volume_payload(90))
    fetch_ohlc("ethereum", days=90, session=sess, cache_dir=str(tmp_path), sleep=0)
    assert (tmp_path / "ethereum.parquet").exists()

    # Second call must hit the fresh cache — a failing session proves no network.
    class _Boom:
        def get(self, *a, **k):
            raise AssertionError("network touched despite fresh cache")

    df = fetch_ohlc("ethereum", days=90, session=_Boom(), cache_dir=str(tmp_path), sleep=0)
    assert len(df) == 90
    assert isinstance(df, pd.DataFrame)

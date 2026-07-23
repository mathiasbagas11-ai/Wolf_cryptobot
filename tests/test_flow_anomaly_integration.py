"""Phase 7 — flow report ↔ anomaly scanner integration (no network)."""

from __future__ import annotations

from wolf.reports.flow import FlowReporter


class _FakeScanner:
    def __init__(self):
        self.verdicts = []

    def build_section(self, verdict):
        self.verdicts.append(verdict)
        return f"🔍 <b>ANOMALY SCANNER</b> [{verdict}]"


def test_section_absent_when_no_scanner():
    assert FlowReporter()._anomaly_section("RISK-ON") == ""


def test_stance_mapped_to_verdict():
    fake = _FakeScanner()
    r = FlowReporter(anomaly=fake)
    assert "[BULLISH]" in r._anomaly_section("RISK-ON (contrarian)")
    assert "[BEARISH]" in r._anomaly_section("RISK-OFF")
    assert "[NEUTRAL]" in r._anomaly_section("ROTATION")
    assert fake.verdicts == ["BULLISH", "BEARISH", "NEUTRAL"]


def test_scan_failure_falls_back_and_never_raises():
    class Boom:
        def build_section(self, verdict):
            raise RuntimeError("coingecko 429")

    out = FlowReporter(anomaly=Boom())._anomaly_section("NEUTRAL")
    assert out.startswith("⚠️ Anomaly scan gagal:")
    assert "coingecko 429" in out


def test_fallback_html_escaped():
    class Boom:
        def build_section(self, verdict):
            raise RuntimeError("<b>bad</b> & ugly")

    out = FlowReporter(anomaly=Boom())._anomaly_section("NEUTRAL")
    assert "&lt;b&gt;bad&lt;/b&gt; &amp; ugly" in out
    assert "<b>bad</b>" not in out

"use client";

import { useEffect, useRef, useState } from "react";

// v2's own rule drawn with Vela's built-in (native) indicators, so the chart
// shows exactly what the bot reads and nothing it does not:
//   regime  BTC 4h + SMA(300)            — wolf/trendtest.py REGIME_BARS
//   coin    Donchian(20) + ATR(14, SMA)  — BREAKOUT_BARS, bbtest._atr_series
// The lengths come from the bot's /v2 `rules`, never typed twice here.
//
// Display only. Nothing on this chart feeds a decision: v2's rules are locked
// for its forward run (registry `trend-breakout-forward-paper`).
//
// Vela (Apache-2.0 + NOTICE) requires a visible attribution wherever a chart
// renders; its built-in logomark is left on, and the caption links it too.
const BINANCE = "https://data-api.binance.vision/api/v3/klines";
const BAR_MS = 4 * 3600 * 1000;

const bar = (iso) => {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? Math.floor(t / BAR_MS) * BAR_MS : null;
};

export default function VelaChart({ symbol, mode = "position", rules = {}, pos = null }) {
  const box = useRef(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    let chart = null;
    let cancelled = false;
    const regimeBars = rules.regime_bars || 300;
    const limit = mode === "regime" ? Math.min(1000, regimeBars + 200) : 300;

    (async () => {
      try {
        const [{ Vela }, rows] = await Promise.all([
          import("@luxalgo/vela"),
          fetch(`${BINANCE}?symbol=${encodeURIComponent(symbol)}&interval=4h&limit=${limit}`)
            .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${symbol} tidak ada di Binance`)))),
        ]);
        if (cancelled) return;
        const data = rows.map((k) => ({
          time: k[0], open: +k[1], high: +k[2], low: +k[3], close: +k[4], volume: +k[5],
        }));
        chart = new Vela(el, { data, timeframe: "240", theme: "dark", upColor: "#26a269", downColor: "#e5484d" });
        await chart.ready();
        if (cancelled) return;
        chart.drawings.showToolbar(false); // a reading view, not a drawing board

        if (mode === "regime") {
          chart.addNativeIndicator("moving-average", { inputs: { maType: "SMA", length: regimeBars } });
          return;
        }

        chart.addNativeIndicator("donchian-channels", { inputs: { length: rules.breakout_bars || 20 } });
        chart.addNativeIndicator("average-true-range", { inputs: { length: 14, smoothing: "SMA" } });

        if (!pos) return;
        const t0 = data[0].time;
        const tN = data[data.length - 1].time;
        // Unlabelled on purpose: four hline labels pile up on the price axis.
        // The caption under the chart names each colour.
        const hline = (price, color, lineStyle = "solid") => price && chart.drawings.add("hline", {
          anchors: [{ time: tN, price }],
          style: { lineColor: color, lineWidth: 1, lineStyle },
        });
        hline(pos.entry, "#4c8dff");
        hline(pos.stop, "#e5484d", "dashed");
        if (pos.initial_stop !== pos.stop) hline(pos.initial_stop, "#8b939e", "dotted");
        hline(pos.level, "#e8b339", "dotted");

        const marks = [];
        const sig = bar(pos.signal_time);
        const fill = bar(pos.opened);
        if (sig !== null && sig >= t0) marks.push({
          id: "signal", time: sig, title: "Sinyal",
          glyph: { shape: "diamond", color: "#e8b339", letter: "S" },
          tooltip: `Candle tutup ${pos.direction === "LONG" ? "di atas" : "di bawah"} channel ${rules.breakout_bars || 20} candle`,
        });
        if (fill !== null && fill !== sig && fill >= t0) marks.push({
          id: "entry", time: fill, title: "Entry",
          glyph: { shape: "circle", color: "#4c8dff", letter: "E" },
          tooltip: "Masuk di open candle berikutnya",
        });
        if (marks.length) chart.marks.set(marks);
      } catch (e) {
        if (!cancelled) setError(e.message === "Failed to fetch"
          ? "Chart gagal dimuat — Binance tidak bisa dihubungi dari jaringan ini" : e.message);
      }
    })();

    return () => { cancelled = true; chart?.destroy(); };
  }, [symbol, mode, rules.breakout_bars, rules.regime_bars, pos]);

  return (
    <>
      {error && <p className="sub">{error}</p>}
      <div ref={box} className="chart vela" />
      <div className="sub" style={{ fontSize: 11, marginTop: 4 }}>
        Chart: <a href="https://velacharts.dev" target="_blank" rel="noreferrer">Vela™ by LuxAlgo</a>
      </div>
    </>
  );
}

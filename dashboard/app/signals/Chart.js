"use client";

import { useEffect, useRef, useState } from "react";
import { createChart, CandlestickSeries, LineStyle } from "lightweight-charts";

// Candles come straight from Binance's public market-data host in the
// viewer's browser: no key, CORS-enabled, and it keeps the bot's own rate
// limit out of it. A symbol Binance does not list just shows a message.
const BINANCE = "https://data-api.binance.vision/api/v3/klines";
const INTERVALS = new Set(["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]);

export default function Chart({ symbol, timeframe, entry, sl, tps }) {
  const box = useRef(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const chart = createChart(el, {
      autoSize: true,
      layout: { background: { color: "#171b21" }, textColor: "#8b939e" },
      grid: { vertLines: { color: "#20252d" }, horzLines: { color: "#20252d" } },
      timeScale: { timeVisible: true, secondsVisible: false },
      localization: { locale: "id-ID" },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#26a269", downColor: "#e5484d", borderVisible: false,
      wickUpColor: "#26a269", wickDownColor: "#e5484d",
    });
    const line = (price, color, title, style = LineStyle.Solid) =>
      price && series.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title });
    line(entry, "#4c8dff", "Entry");
    line(sl, "#e5484d", "SL", LineStyle.Dashed);
    (tps || []).forEach((p, i) => line(p, "#26a269", `TP${i + 1}`, LineStyle.Dotted));

    const interval = INTERVALS.has(timeframe) ? timeframe : "15m";
    let cancelled = false;
    fetch(`${BINANCE}?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=200`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${symbol} tidak ada di Binance`))))
      .then((rows) => {
        if (cancelled) return;
        series.setData(rows.map((k) => ({
          time: Math.floor(k[0] / 1000), open: +k[1], high: +k[2], low: +k[3], close: +k[4],
        })));
        chart.timeScale().fitContent();
      })
      .catch((e) => !cancelled && setError(
        e.message === "Failed to fetch" ? "Chart gagal dimuat — Binance tidak bisa dihubungi dari jaringan ini" : e.message,
      ));

    return () => { cancelled = true; chart.remove(); };
  }, [symbol, timeframe, entry, sl, tps]);

  return (
    <>
      {error && <p className="sub">{error}</p>}
      <div ref={box} className="chart" />
    </>
  );
}

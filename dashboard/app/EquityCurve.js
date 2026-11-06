"use client";

import { useEffect, useRef } from "react";
import { createChart, LineSeries, LineStyle } from "lightweight-charts";

export default function EquityCurve({ points }) {
  const box = useRef(null);
  useEffect(() => {
    if (!box.current || !points?.length) return;
    const chart = createChart(box.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#8a94a3", fontSize: 11 },
      grid: { vertLines: { visible: false }, horzLines: { color: "#1c222b" } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true },
      // An explicit locale: the chart otherwise formats dates with the browser's
      // own tag, and a malformed one throws and leaves the chart blank.
      localization: { locale: "id-ID", priceFormatter: (p) => `${p.toFixed(1)}%` },
    });
    // Strictly increasing times are required; trades resolving in the same
    // second are nudged apart rather than dropped.
    let last = 0;
    const data = points.map((p) => { const t = Math.max(p.t, last + 1); last = t; return { ...p, t }; });
    chart.addSeries(LineSeries, { color: "#5d6675", lineWidth: 1, lineStyle: LineStyle.Dashed,
      priceLineVisible: false, lastValueVisible: false })
      .setData(data.map((p) => ({ time: p.t, value: p.gross })));
    const net = chart.addSeries(LineSeries, { color: "#5b9dff", lineWidth: 2, priceLineVisible: false });
    net.setData(data.map((p) => ({ time: p.t, value: p.net })));
    net.createPriceLine({ price: 0, color: "#3a4352", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false });
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points]);
  return (
    <>
      <div ref={box} className="chart" />
      <div className="legend">
        <span><i style={{ background: "#5b9dff" }} />Setelah biaya</span>
        <span><i style={{ background: "#5d6675" }} />Sebelum biaya</span>
      </div>
    </>
  );
}

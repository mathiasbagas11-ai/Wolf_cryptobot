import { wolf, fmt, price } from "../../lib/wolf";
import Chart from "./Chart";

export const dynamic = "force-dynamic";

const age = (iso) => {
  if (!iso) return "";
  const h = (Date.now() - new Date(iso).getTime()) / 36e5;
  return h < 1 ? `${Math.round(h * 60)} mnt` : `${h.toFixed(1)} jam`;
};

export default async function Signals() {
  const data = await wolf("/signals/active");
  if (data.error) return (<><h1>Sinyal aktif</h1><p className="err">{data.error}</p></>);
  const signals = [...data.signals].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));

  return (
    <>
      <h1>Sinyal aktif <span className="sub">({data.count})</span></h1>
      <p className="note">
        Semua sinyal yang lagi jalan di paper ledger. Saran saat ini: jangan diikuti pakai uang beneran.
      </p>
      {signals.length === 0 && <p className="sub">Tidak ada posisi terbuka.</p>}
      {signals.map((s) => {
        const risk = s.entry_price ? (Math.abs(s.entry_price - s.sl) / s.entry_price) * 100 : null;
        const ladder = (s.tp_ladder || []).map((t) => t.price).filter(Boolean);
        return (
          <article className="card" key={s.id}>
            <div>
              <strong>{s.symbol}</strong>{" "}
              <span className={`pill ${s.direction === "LONG" ? "long" : "short"}`}>{s.direction}</span>
              <span className="pill">{s.strategy}</span>
              <span className="pill">{s.timeframe}</span>
              <span className="pill">{s.status}</span>
            </div>
            <div className="sub" style={{ marginTop: 6 }}>
              Entry {price(s.entry_price)} · SL {price(s.sl)}
              {risk !== null && ` (1R = ${fmt(risk, 2)}%)`}
              {ladder.length > 0 && ` · TP ${ladder.map((p) => price(p)).join(" / ")}`}
              {s.tps_hit?.length > 0 && ` · TP kena: ${s.tps_hit.join(", ")}`}
            </div>
            <div className="sub">
              Umur {age(s.created_at)}
              {s.spread_bps !== null && s.spread_bps !== undefined && ` · spread ${fmt(s.spread_bps, 1)}bps`}
              {s.whale_stance && ` · whale ${s.whale_stance}`}
            </div>
            <details>
              <summary>Lihat chart</summary>
              <Chart
                symbol={s.symbol}
                timeframe={s.timeframe}
                entry={s.entry_price}
                sl={s.sl}
                tps={ladder}
              />
            </details>
          </article>
        );
      })}
    </>
  );
}

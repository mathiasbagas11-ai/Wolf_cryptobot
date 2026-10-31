import { wolf, fmt, price } from "../../lib/wolf";
import Chart from "./Chart";
import { STRATEGY, ENTRY_MODE, parts } from "../../lib/explain";

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
      {signals.map((s, idx) => {
        const risk = s.entry_price ? (Math.abs(s.entry_price - s.sl) / s.entry_price) * 100 : null;
        const ladder = (s.tp_ladder || []).map((t) => t.price).filter(Boolean);
        const why = STRATEGY[(s.strategy || "").toUpperCase()];
        const awarded = parts(s.score_parts);
        const waiting = s.entry_mode === "RETEST_WAIT" && !s.activated;
        return (
          <article className="card" key={s.id}>
            <div>
              <strong>{s.symbol}</strong>{" "}
              <span className={`pill ${s.direction === "LONG" ? "long" : "short"}`}>{s.direction}</span>
              <span className="pill">{s.strategy}</span>
              <span className="pill">{s.timeframe}</span>
              <span className="pill">{waiting ? "nunggu retest" : s.status}</span>
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
            <div className="why">
              <div className="why-title">Kenapa masuk di sini</div>
              {why && <p>{why.idea}</p>}
              <ul>
                {why && <li><b>Entry:</b> {why.entry}{ENTRY_MODE[s.entry_mode] ? ` (${ENTRY_MODE[s.entry_mode]})` : ""}</li>}
                {why && <li><b>Stop:</b> {why.stop}</li>}
                {ladder.length > 0 && <li><b>TP:</b> kelipatan jarak SL (1R) dari entry; sebagian posisi ditutup di tiap TP.</li>}
              </ul>
              {awarded.length > 0 && (
                <>
                  <div className="why-sub">Syarat yang terpenuhi · skor {s.score}</div>
                  <div className="chips">
                    {awarded.map((a) => <span className="chip" key={a.key}>{a.label} <b>+{a.pts}</b></span>)}
                  </div>
                </>
              )}
              {s.reasons?.length > 0 && (
                <div className="raw">Catatan bot: {s.reasons.join(" · ")}</div>
              )}
              <div className="raw">Ini alasan aturan bot masuk, bukan bukti untung — belum ada strategi yang terbukti punya edge.</div>
            </div>
            <details open={idx === 0}>
              <summary>Lihat chart</summary>
              <Chart
                symbol={s.symbol}
                timeframe={s.timeframe}
                entry={s.entry_price}
                sl={s.sl}
                tps={ladder}
                isLong={s.direction === "LONG"}
                createdAt={s.created_at}
                activatedAt={s.activated ? s.activated_at : null}
              />
            </details>
          </article>
        );
      })}
    </>
  );
}

import { wolf, fmt, signed, price } from "../../lib/wolf";
import EquityCurve from "../EquityCurve";
import Chart from "../signals/Chart";

export const dynamic = "force-dynamic";

const tone = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const when = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");
const short = (iso) => (iso ? iso.slice(5, 16).replace("T", " ") : "—");

function Tile({ label, value, hint, cls = "" }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className={`value ${cls}`}>{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export default async function V2() {
  const d = await wolf("/v2");
  if (d.error) return (<><h1>Strategi v2</h1><p className="err">
    {/404/.test(d.error)
      ? "Bot di Railway belum punya v2. Merge & deploy commit terbaru, lalu refresh."
      : `Bot: ${d.error}`}
  </p></>);
  if (!d.enabled) return (<><h1>Strategi v2</h1><p className="sub">v2 sedang dimatikan (V2_ENABLED=false).</p></>);

  const r = d.rules;
  return (
    <>
      <h1>🧪 Strategi v2 <span className="sub">trend breakout · paper</span></h1>
      <p className="note">
        <b>Belum lolos tes.</b> Di backtest 6 tahun aturan ini ngalahin 95% versi acak, syaratnya 97.5%.
        Dijalanin di data baru buat dicek, bukan buat diikuti. Jangan pakai uang beneran.
      </p>
      <p className="lede">
        Mulai {when(d.started)} UTC · saldo awal {fmt(d.start_balance, 0)} · {d.risk_pct}% risk per trade ·
        semua angka setelah biaya {d.cost_bps}bps
      </p>

      <div className="tiles">
        <Tile label="Saldo paper" value={fmt(d.balance)} cls={tone(d.return_pct)}
          hint={`${signed(d.return_pct)}% dari ${fmt(d.start_balance, 0)}`} />
        <Tile label="Hasil per trade" value={d.net_r === null ? "—" : `${signed(d.net_r, 3)}R`} cls={tone(d.net_r)}
          hint={d.gross_r === null ? "belum ada trade selesai" : `sebelum biaya ${signed(d.gross_r, 3)}R`} />
        <Tile label="Win rate" value={d.n ? `${fmt((d.wins / d.n) * 100, 0)}%` : "—"}
          hint="wajar rendah (30–40%): tren menang jarang tapi besar" />
        <Tile label="Trade selesai" value={`${d.n} / ${d.kill_after}`}
          cls={d.killed ? "neg" : ""}
          hint={d.killed ? "aturan stop kena — v2 berhenti" : "cek di 300: rata-rata < 0 → berhenti"} />
      </div>

      <section className="card">
        <h2>Kurva saldo <span>% dari awal</span></h2>
        {d.curve.length ? <EquityCurve points={d.curve} /> : <p className="sub">Belum ada trade yang selesai.</p>}
        <div className="sub" style={{ marginTop: 8 }}>
          LONG {d.long.n} trade{d.long.net_r !== null && ` · rata-rata ${signed(d.long.net_r, 3)}R`} ·
          SHORT {d.short.n} trade{d.short.net_r !== null && ` · rata-rata ${signed(d.short.net_r, 3)}R`}
        </div>
        {d.by_label && (
          <div className="sub">
            Label tren (cuma dicatat, dibaca di {d.kill_after} trade): PERSIST {d.by_label.PERSIST.n} trade
            {d.by_label.PERSIST.net_r !== null && ` · ${signed(d.by_label.PERSIST.net_r, 3)}R`} ·
            NOT {d.by_label.NOT.n} trade{d.by_label.NOT.net_r !== null && ` · ${signed(d.by_label.NOT.net_r, 3)}R`}
          </div>
        )}
      </section>

      <section className="card">
        <h2>Aturannya <span>dikunci, tidak diubah selama jalan</span></h2>
        <ul className="sub">
          <li><b>Arah pasar:</b> BTC di atas rata-rata ~50 hari → cuma long. Di bawah → cuma short.</li>
          <li><b>Entry:</b> candle 4h tutup di atas high {r.breakout_bars} candle (long) / di bawah low {r.breakout_bars} candle (short), masuk di open candle berikutnya.</li>
          <li><b>Stop:</b> {r.stop_atr}×ATR. <b>Exit:</b> trailing {r.trail_atr}×ATR dari harga terbaik, tanpa TP, maks {r.hold_days} hari.</li>
          <li><b>Koin:</b> 10 koin besar. Satu posisi per koin.</li>
        </ul>
      </section>

      <h2 style={{ margin: "18px 0 8px" }}>Posisi terbuka <span className="sub">({d.open.length})</span></h2>
      {d.open.length === 0 && <p className="sub">Tidak ada posisi. Nunggu breakout yang searah regime BTC.</p>}
      {d.open.map((p, idx) => (
        <article className="card" key={p.symbol}>
          <div>
            <strong>{p.symbol}</strong>{" "}
            <span className={`pill ${p.direction === "LONG" ? "long" : "short"}`}>{p.direction}</span>
            <span className="pill">4h</span>
            {p.persist && <span className="pill">{p.persist.label} · VR {fmt(p.persist.vr, 2)}</span>}
          </div>
          <div className="sub" style={{ marginTop: 6 }}>
            Entry {price(p.entry)} · stop sekarang {price(p.stop)} · stop awal {price(p.initial_stop)} (1R = {fmt(p.risk_pct, 2)}%)
          </div>
          <div className="sub">Masuk {when(p.opened)} UTC · level breakout {price(p.level)}</div>
          <details open={idx === 0}>
            <summary>Lihat chart</summary>
            <Chart symbol={p.symbol} timeframe="4h" entry={p.entry} sl={p.stop} tps={[]}
              isLong={p.direction === "LONG"} createdAt={p.signal_time} activatedAt={p.opened} />
          </details>
        </article>
      ))}

      <h2 style={{ margin: "18px 0 8px" }}>Trade selesai <span className="sub">(terbaru dulu)</span></h2>
      {d.closed.length === 0 ? <p className="sub">Belum ada.</p> : (
        <div className="card scroll">
          <table>
            <thead><tr><th>Koin</th><th>Arah</th><th>Tutup</th><th>Cara</th>
              <th>Label</th><th className="num">R</th><th className="num">R net</th></tr></thead>
            <tbody>
              {d.closed.map((t, i) => (
                <tr key={i}>
                  <td>{t.symbol}</td>
                  <td className={t.direction === "LONG" ? "pos" : "neg"}>{t.direction}</td>
                  <td style={{ whiteSpace: "nowrap" }}>{short(t.exit_time)}</td>
                  <td>{t.how === "stop" ? "trailing" : "15 hari"}</td>
                  <td className="sub">{t.label || "—"}</td>
                  <td className="num">{signed(t.r, 2)}</td>
                  <td className={`num ${tone(t.net_r)}`}>{signed(t.net_r, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

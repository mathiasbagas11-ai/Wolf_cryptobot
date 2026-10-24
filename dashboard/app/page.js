import { wolf, fmt, signed } from "../lib/wolf";

export const dynamic = "force-dynamic";

const tone = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

export default async function Overview() {
  const [paper, mc, health] = await Promise.all([
    wolf("/paper"), wolf("/montecarlo"), wolf("/health"),
  ]);

  return (
    <>
      <h1>Overview</h1>
      {health.error && <p className="err">Bot: {health.error}</p>}

      <section className="card">
        <h2>Saldo paper</h2>
        {paper.error ? <p className="err">{paper.error}</p> : (
          <div className="grid">
            <div>
              <div className="sub">Setelah biaya {paper.net_cost_bps ? `(${paper.net_cost_bps}bps)` : ""}</div>
              {paper.net_balance !== undefined ? (
                <>
                  <div className={`big ${tone(paper.net_return_pct)}`}>{fmt(paper.net_balance)}</div>
                  <div className="sub">
                    {signed(paper.net_return_pct)}% sejak {paper.net_since} · {paper.net_trades} trade ·
                    kotor {signed(paper.gross_return_since_pct)}% di trade yang sama
                  </div>
                </>
              ) : <div className="sub">Mulai dihitung setelah trade berikutnya selesai.</div>}
            </div>
            <div>
              <div className="sub">Saldo kotor (tanpa fee)</div>
              <div className="big">{fmt(paper.balance)}</div>
              <div className="sub">{signed(paper.return_pct)}% · {paper.trades} trade</div>
            </div>
            {paper.ledger && (
              <div>
                <div className="sub">Log {paper.ledger.first_day} → {paper.ledger.last_day}</div>
                <div className={`big ${tone(paper.ledger.net_pct)}`}>{signed(paper.ledger.net_pct, 1)}%</div>
                <div className="sub">
                  bersih · kotor {signed(paper.ledger.gross_pct, 1)}% · max DD {fmt(paper.ledger.net_max_dd, 1)}% ·{" "}
                  {paper.ledger.trades} trade
                </div>
              </div>
            )}
          </div>
        )}
      </section>

      <section className="card">
        <h2>Monte Carlo — 30 hari ke depan, semua strategi</h2>
        {mc.error ? <p className="err">{mc.error}</p> : mc.return ? (
          <>
            <div className="grid">
              <div><div className="sub">Skenario jelek (p5)</div><div className={`big ${tone(mc.return.p5)}`}>{signed(mc.return.p5, 1)}%</div></div>
              <div><div className="sub">Tengah</div><div className={`big ${tone(mc.return.p50)}`}>{signed(mc.return.p50, 1)}%</div></div>
              <div><div className="sub">Skenario bagus (p95)</div><div className={`big ${tone(mc.return.p95)}`}>{signed(mc.return.p95, 1)}%</div></div>
              <div><div className="sub">Turun terdalam yang wajar (p95)</div><div className="big neg">−{fmt(mc.max_dd.p95, 1)}%</div></div>
            </div>
            <p className="sub">
              {mc.p_loss}% jalur berakhir rugi · {mc.trades} trade, {mc.days} hari · {mc.risk_pct}% risk per trade ·
              rata-rata {signed(mc.mean_gross_r, 3)}R kotor / {signed(mc.mean_net_r, 3)}R bersih
            </p>
            <div className="scroll">
              <table>
                <thead><tr><th>Minggu</th><th>Trade</th><th>Kotor (R)</th><th>Bersih (R)</th></tr></thead>
                <tbody>
                  {mc.weeks.map((w) => (
                    <tr key={w.week}>
                      <td>{w.week}</td><td>{w.n}</td>
                      <td className={tone(w.gross)}>{signed(w.gross, 3)}</td>
                      <td className={tone(w.net)}>{signed(w.net, 3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : <p className="sub">{mc.error || mc.message || "Data belum cukup (butuh minimal 7 hari trading)."}</p>}
      </section>

      <p className="note">
        Ini alat ukur, bukan sinyal beli. Belum ada strategi yang terbukti untung setelah biaya —
        lihat <code>/tested</code> di Telegram. Hasil yang bagus beberapa hari belum berarti apa-apa.
      </p>
    </>
  );
}

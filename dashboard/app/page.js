import { wolf, fmt, signed } from "../lib/wolf";
import EquityCurve from "./EquityCurve";
import Calendar from "./Calendar";

export const dynamic = "force-dynamic";

const tone = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

function Tile({ label, value, hint, cls = "" }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className={`value ${cls}`}>{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

function Bar({ value, max }) {
  const w = Math.min(50, (Math.abs(value) / (max || 1)) * 50);
  const style = value >= 0
    ? { left: "50%", width: `${w}%`, background: "var(--green)" }
    : { right: "50%", width: `${w}%`, background: "var(--red)" };
  return <div className="bar"><span style={style} /><div className="mid" /></div>;
}

export default async function Overview() {
  const [paper, j, mc] = await Promise.all([wolf("/paper"), wolf("/journal"), wolf("/montecarlo")]);
  if (j.error) return (<><h1>Overview</h1><p className="err">Bot: {j.error}</p></>);
  if (j.empty) return (<><h1>Overview</h1><p className="sub">Belum ada trade yang selesai di log.</p></>);

  const o = j.overall;
  const strategies = Object.entries(j.by_strategy);
  const maxAbs = Math.max(...strategies.map(([, s]) => Math.abs(s.net_r)), 0.1);

  return (
    <>
      <h1>Overview</h1>
      <p className="lede">
        Log {j.from} → {j.to} · {o.n} trade · {j.risk_pct}% risk per trade · semua angka setelah biaya {j.cost_bps}bps
      </p>

      <div className="tiles">
        <Tile label="Saldo bersih"
          value={paper.net_balance !== undefined ? fmt(paper.net_balance) : "—"}
          cls={tone(paper.net_return_pct)}
          hint={paper.net_balance !== undefined
            ? `${signed(paper.net_return_pct)}% sejak ${paper.net_since} · kotor ${fmt(paper.balance)}`
            : "mulai setelah trade berikutnya"} />
        <Tile label="Hasil per trade" value={`${signed(o.net_r, 3)}R`} cls={tone(o.net_r)}
          hint={`sebelum biaya ${signed(o.gross_r, 3)}R`} />
        <Tile label="Win rate" value={`${fmt(o.win_rate, 1)}%`}
          cls={o.breakeven_wr && o.win_rate >= o.breakeven_wr ? "pos" : "neg"}
          hint={o.breakeven_wr ? `butuh ${fmt(o.breakeven_wr, 1)}% buat impas` : ""} />
        <Tile label="Profit factor" value={o.profit_factor ?? "—"}
          cls={o.profit_factor >= 1 ? "pos" : "neg"} hint="di bawah 1 = rugi" />
        <Tile label="Turun terdalam" value={`−${fmt(o.max_dd_pct, 1)}%`} cls="neg"
          hint={`log ${signed(o.net_return_pct, 1)}% bersih`} />
      </div>

      <section className="card">
        <h2>Equity <small>dari {j.from}, {j.risk_pct}% per trade</small></h2>
        <EquityCurve points={j.curve} />
      </section>

      <section className="card">
        <h2>Kalender profit <small>R bersih per hari (WIB) · total mingguan di kanan</small></h2>
        <Calendar days={j.days} />
      </section>

      <section className="card">
        <h2>Per strategi <small>belum ada yang terbukti beda dari kebetulan</small></h2>
        <div className="scroll">
          <table>
            <thead><tr>
              <th>Strategi</th><th className="num">Trade</th><th className="num">Win</th>
              <th className="num">Kotor</th><th className="num">Bersih</th><th className="num">PF</th><th className="hide-sm"></th>
            </tr></thead>
            <tbody>
              {strategies.map(([name, s]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td className="num">{s.n}</td>
                  <td className="num">{fmt(s.win_rate, 0)}%</td>
                  <td className={`num ${tone(s.gross_r)}`}>{signed(s.gross_r, 3)}</td>
                  <td className={`num ${tone(s.net_r)}`}>{signed(s.net_r, 3)}</td>
                  <td className="num">{s.profit_factor ?? "—"}</td>
                  <td className="hide-sm" style={{ width: "28%" }}><Bar value={s.net_r} max={maxAbs} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {mc.return && (
        <section className="card">
          <h2>30 hari ke depan <small>simulasi Monte Carlo · {mc.p_loss}% skenario rugi</small></h2>
          <div className="tiles" style={{ marginBottom: 0 }}>
            <Tile label="Jelek (p5)" value={`${signed(mc.return.p5, 1)}%`} cls={tone(mc.return.p5)} />
            <Tile label="Tengah" value={`${signed(mc.return.p50, 1)}%`} cls={tone(mc.return.p50)} />
            <Tile label="Bagus (p95)" value={`${signed(mc.return.p95, 1)}%`} cls={tone(mc.return.p95)} />
            <Tile label="Turun terdalam (p95)" value={`−${fmt(mc.max_dd.p95, 1)}%`} cls="neg" />
          </div>
        </section>
      )}

      <p className="note">
        Alat ukur, bukan sinyal beli. Hari hijau di kalender itu wajar walaupun bot rugi —
        lihat total, bukan hari terbaiknya.
      </p>
    </>
  );
}

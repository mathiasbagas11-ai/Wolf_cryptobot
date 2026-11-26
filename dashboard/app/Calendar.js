// Profit calendar: one grid per month, Monday first, each day's net R and
// trade count, with the week's total in the last column — the layout journal
// tools use, fed by days already bucketed in the owner's timezone.

const DOW = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"];
const MONTHS = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
  "Agustus", "September", "Oktober", "November", "Desember"];

function shade(r) {
  if (r === undefined) return undefined;
  const a = Math.min(0.55, 0.12 + Math.abs(r) / 12);
  return r >= 0 ? `rgba(34,197,139,${a})` : `rgba(240,86,106,${a})`;
}

function monthGrid(year, month, byDate) {
  const first = new Date(Date.UTC(year, month, 1));
  const lead = (first.getUTCDay() + 6) % 7;            // Monday = 0
  const days = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const cells = [];
  for (let i = 0; i < lead; i++) cells.push(null);
  for (let d = 1; d <= days; d++) {
    const key = `${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    cells.push({ d, ...(byDate[key] || {}) });
  }
  while (cells.length % 7) cells.push(null);
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return weeks;
}

export default function Calendar({ days }) {
  const byDate = Object.fromEntries(days.map((d) => [d.date, d]));
  const months = [...new Set(days.map((d) => d.date.slice(0, 7)))];
  return months.map((ym) => {
    const [y, m] = ym.split("-").map(Number);
    const weeks = monthGrid(y, m - 1, byDate);
    return (
      <div key={ym}>
        <div className="month">{MONTHS[m - 1]} {y}</div>
        <div className="cal">
          {DOW.map((d) => <div key={d} className="dow">{d}</div>)}
          <div className="dow">Total</div>
          {weeks.map((w, wi) => {
            const traded = w.filter((c) => c && c.n);
            const total = traded.reduce((s, c) => s + c.net_r, 0);
            const n = traded.reduce((s, c) => s + c.n, 0);
            return [
              ...w.map((c, ci) => c ? (
                <div key={`${wi}-${ci}`} className="day" style={{ background: shade(c.net_r) }}>
                  <div className="d">{c.d}</div>
                  {c.n ? (
                    <>
                      <div className="r">{c.net_r >= 0 ? "+" : ""}{c.net_r.toFixed(1)}R</div>
                      <div className="n">{c.n} trade</div>
                    </>
                  ) : null}
                </div>
              ) : <div key={`${wi}-${ci}`} className="day out" />),
              <div key={`${wi}-w`} className="wk">
                {n ? (
                  <>
                    <div className={total >= 0 ? "pos" : "neg"} style={{ fontWeight: 700 }}>
                      {total >= 0 ? "+" : ""}{total.toFixed(1)}R
                    </div>
                    <div className="muted">{n} trade</div>
                  </>
                ) : <span className="muted">—</span>}
              </div>,
            ];
          })}
        </div>
      </div>
    );
  });
}

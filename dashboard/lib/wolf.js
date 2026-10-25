import "server-only";

// Every call to the bot happens on the server, so WOLF_API_KEY never reaches
// the browser. no-store because a paper ledger that is a minute stale is the
// wrong number to read a position against.
export async function wolf(path) {
  const raw = (process.env.WOLF_API_URL || "").trim();
  if (!raw) return { error: "WOLF_API_URL is not set in Vercel" };
  // Railway shows its domain without a scheme, and that is what gets pasted.
  const base = (/^https?:\/\//i.test(raw) ? raw : `https://${raw}`).replace(/\/+$/, "");
  try {
    const res = await fetch(`${base}${path}`, {
      headers: { "X-API-Key": process.env.WOLF_API_KEY || "" },
      cache: "no-store",
    });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch {}
      return { error: `${res.status} ${detail || res.statusText}` };
    }
    return await res.json();
  } catch (e) {
    return { error: `bot unreachable: ${e.message}` };
  }
}

export const fmt = (v, d = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d);

export const signed = (v, d = 2) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(d)}`;

// Prices span BTC at 100k and a meme coin at 0.00001, so a fixed number of
// decimals is wrong at one end or the other.
export const price = (v) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  if (Math.abs(n) >= 1000) return n.toFixed(2);
  if (Math.abs(n) >= 1) return String(Number(n.toFixed(4)));
  return String(Number(n.toPrecision(5)));
};

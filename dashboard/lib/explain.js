// Plain-language reading of why a detector took a signal, for the Sinyal
// aktif page. Everything here is a translation of what the detector code in
// wolf/detectors/ actually checks; when a detector changes, change this too.
// It explains the rule, never vouches for it: no strategy has a measured edge.

export const STRATEGY = {
  MOMENTUM: {
    idea: "Breakout: harga baru tembus level tertinggi/terendah 50 candle terakhir dengan volume besar.",
    entry: "Masuk langsung di harga saat breakout terkonfirmasi.",
    stop: "SL 1,5× ATR (rata-rata gerak per candle) dari entry.",
  },
  SCALP: {
    idea: "Stop-hunt: harga nyolok lewat swing terakhir buat nyapu stop orang, lalu balik lagi.",
    entry: "Masuk langsung setelah harga balik ke dalam range.",
    stop: "SL sedikit di luar ujung colokan (0,25× ATR).",
  },
  TRAP: {
    idea: "Jebakan breakout: harga nembus level jelas lalu langsung balik — yang beli breakout jadi korban.",
    entry: "Masuk langsung, berlawanan arah dengan breakout palsunya.",
    stop: "SL sedikit di luar ujung colokan (0,25× ATR).",
  },
  SWING: {
    idea: "Ikut tren: harga lagi tren, koreksi ke EMA20, lalu muncul candle penolakan.",
    entry: "Nunggu harga balik ke EMA20 dulu; sinyal baru aktif kalau harga menyentuh level itu.",
    stop: "SL di bawah/atas sumbu candle penolakan, minimal 0,8× ATR dari entry.",
  },
  PREDUMP: {
    idea: "Tanda distribusi di dekat puncak: RSI kepanasan, sumbu atas panjang, volume melemah — short sebelum turun.",
    entry: "Masuk short langsung di harga sekarang.",
    stop: "SL 1,5× ATR di atas entry.",
  },
  PREPUMP: {
    idea: "Akumulasi sebelum naik: Bollinger menyempit lalu volume mulai masuk.",
    entry: "Masuk long langsung di harga sekarang.",
    stop: "SL 1,5× ATR di bawah entry, atau di bawah dasar konsolidasi kalau lebih rendah.",
  },
};

export const ENTRY_MODE = {
  MOMENTUM_NOW: "Entry langsung",
  RETEST_WAIT: "Nunggu retest",
};

// score_parts keys → what the condition means. Unknown keys fall back to the key.
const PARTS = {
  breakout: "Tembus level 50 candle",
  macd_confirms: "MACD searah",
  volume_surge: "Volume melonjak",
  volume_ok: "Volume di atas rata-rata",
  vwap_aligned: "Searah VWAP",
  structure_break: "Struktur patah (BOS/ChoCh)",
  not_overextended: "RSI belum kepanasan",
  flow_agrees: "Order flow searah",
  sweep: "Sapu likuiditas (stop-hunt)",
  sweep_reclaim: "Sapu lalu balik ke range",
  volume_spike: "Volume spike saat colokan",
  volume_climax: "Volume klimaks saat colokan",
  volume_elevated: "Volume agak tinggi",
  rsi_extreme: "RSI ekstrem",
  rsi_leaning: "RSI condong ekstrem",
  absorption: "Absorpsi (jual/beli diserap)",
  fvg: "Di dalam Fair Value Gap",
  vwap: "Diskon/premium ke VWAP",
  order_block: "Di dalam Order Block",
  divergence: "Divergensi RSI",
  away_from_value: "Jauh dari harga wajar",
  rejection_wick: "Sumbu penolakan",
  rejection_deep: "Sumbu penolakan dalam",
  rejection_volume: "Volume di candle penolakan",
  trend_alignment: "Tren searah (EMA20 vs EMA50)",
  pullback: "Koreksi ke EMA20",
  vwap_anchor: "Dekat VWAP",
  rsi_compression: "RSI di zona koreksi ideal",
  bear_divergence: "Divergensi bearish RSI",
  overbought_at_high: "RSI overbought di pucuk",
  rsi_elevated: "RSI tinggi",
  bearish_rejection: "Candle penolakan bearish",
  volume_fading: "Volume melemah (distribusi)",
  vwap_premium: "Jauh di atas VWAP",
  bear_fvg_above: "FVG bearish di atas",
  funding_overheated: "Funding kepanasan",
  oi_falling: "Open interest turun",
  bollinger_squeeze: "Bollinger menyempit",
  volume_coil: "Volume mulai masuk setelah sepi",
  volume_building: "Volume membangun",
  momentum_zone: "RSI & MACD mulai naik",
  macd_positive: "MACD positif",
  bull_divergence: "Divergensi bullish RSI",
  trend_context: "Di atas EMA50",
  value_proximity: "Belum jauh dari VWAP",
  bull_fvg_below: "FVG bullish di bawah",
  funding_extreme: "Funding ekstrem negatif",
  funding_negative: "Funding negatif",
  oi_rising: "Open interest naik",
};

export const partLabel = (key) => PARTS[key] || key.replace(/_/g, " ");

// Largest award first, so the condition that carried the score leads.
export const parts = (scoreParts) =>
  Object.entries(scoreParts || {})
    .filter(([, pts]) => pts > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([key, pts]) => ({ key, label: partLabel(key), pts }));

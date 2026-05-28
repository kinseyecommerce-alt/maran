/* ============================================================
   NIRMA TERMINAL — dark pro-trading UI kit
   Theme tokens, primitives, inline icons, mock data engine.
   Aesthetic: neon-on-black HFT terminal. NSE/BSE equities, INR.
   ============================================================ */

// ---------- inline icons (1.75px stroke) ----------
const TPATHS = {
  pulse: <path d="M3 12h4l2-7 4 14 2-7h6" />,
  up:    <><polyline points="22 7 13.5 15.5 8.5 10.5 2 17" /><polyline points="16 7 22 7 22 13" /></>,
  down:  <><polyline points="22 17 13.5 8.5 8.5 13.5 2 7" /><polyline points="16 17 22 17 22 11" /></>,
  bolt:  <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />,
  stop:  <rect x="5" y="5" width="14" height="14" rx="2" />,
  cross: <><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>,
  grid:  <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
  shield:<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10" />,
  search:<><circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></>,
  dot:   <circle cx="12" cy="12" r="5" />,
  layers:<><polygon points="12 2 2 7 12 12 22 7 12 2" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" /></>,
  alert: <><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></>,
  sun:   <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>,
  moon:  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />,
};
function TIcon({ name, size = 16, className = '', style }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" className={className} style={style}>
      {TPATHS[name]}
    </svg>
  );
}

// ---------- formatters ----------
const tcx = (...a) => a.filter(Boolean).join(' ');
const fmt = (v, dp = 2) => Number(v).toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp });
const fmt0 = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });
const sgn = (v) => (v >= 0 ? '+' : '');
const compact = (v) => {
  if (v >= 1e7) return (v / 1e7).toFixed(2) + 'Cr';
  if (v >= 1e5) return (v / 1e5).toFixed(2) + 'L';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return String(Math.round(v));
};

// ---------- mock market data ----------
const TSEED = [
  { sym: 'RELIANCE',  px: 2847.50, seg: 'EQ', rsi: 58 },
  { sym: 'HDFCBANK',  px: 1678.30, seg: 'EQ', rsi: 61 },
  { sym: 'ICICIBANK', px: 1187.90, seg: 'EQ', rsi: 66 },
  { sym: 'TCS',       px: 3912.00, seg: 'EQ', rsi: 42 },
  { sym: 'INFY',      px: 1543.75, seg: 'EQ', rsi: 47 },
  { sym: 'SBIN',      px:  812.45, seg: 'EQ', rsi: 38 },
  { sym: 'BHARTIARTL',px: 1456.20, seg: 'EQ', rsi: 55 },
  { sym: 'TATAMOTORS',px:  978.10, seg: 'EQ', rsi: 71 },
  { sym: 'ADANIENT',  px: 2410.65, seg: 'EQ', rsi: 49 },
  { sym: 'AXISBANK',  px: 1142.80, seg: 'EQ', rsi: 53 },
  { sym: 'WIPRO',     px:  298.35, seg: 'EQ', rsi: 44 },
  { sym: 'MARUTI',    px:11820.00, seg: 'EQ', rsi: 63 },
];

function tSeed() {
  const t = {};
  for (const s of TSEED) {
    const chg = Math.random() * 4 - 2;
    t[s.sym] = {
      symbol: s.sym, seg: s.seg, ltp: s.px, prev: s.px, open: s.px * (1 - chg / 200),
      change_pct: chg, rsi: s.rsi,
      day_high: s.px * 1.014, day_low: s.px * 0.986,
      vwap: s.px * (1 + (Math.random() - 0.5) * 0.004),
      vol: 1e5 + Math.random() * 9e6,
      spark: Array.from({ length: 24 }, () => s.px * (1 + (Math.random() - 0.5) * 0.012)),
    };
  }
  return t;
}

function tStep(prev) {
  const next = {};
  for (const k of Object.keys(prev)) {
    const t = prev[k];
    const drift = (Math.random() - 0.5) * t.ltp * 0.0009;
    const ltp = Math.max(1, t.ltp + drift);
    next[k] = {
      ...t, prev: t.ltp, ltp,
      change_pct: ((ltp - t.open) / t.open) * 100,
      day_high: Math.max(t.day_high, ltp), day_low: Math.min(t.day_low, ltp),
      vol: t.vol + Math.random() * 4000,
      spark: [...t.spark.slice(1), ltp],
    };
  }
  return next;
}

// candlesticks for a symbol
function tCandles(base, n = 64) {
  const out = []; let px = base * 0.982;
  for (let i = 0; i < n; i++) {
    const o = px;
    const c = Math.max(1, o + (Math.random() - 0.47) * base * 0.0045);
    const hi = Math.max(o, c) + Math.random() * base * 0.0022;
    const lo = Math.min(o, c) - Math.random() * base * 0.0022;
    out.push({ o, c, hi, lo, vol: 0.3 + Math.random() * 0.7 });
    px = c;
  }
  return out;
}
function tEma(vals, p) {
  const k = 2 / (p + 1); let e = vals[0]; const out = [e];
  for (let i = 1; i < vals.length; i++) { e = vals[i] * k + e * (1 - k); out.push(e); }
  return out;
}

// depth-of-market ladder around ltp
function tDepth(ltp) {
  const tickSz = ltp > 2000 ? 0.5 : 0.05;
  const bids = [], asks = [];
  for (let i = 1; i <= 8; i++) {
    bids.push({ px: ltp - i * tickSz, sz: Math.round(40 + Math.random() * 900), n: Math.round(1 + Math.random() * 30) });
    asks.push({ px: ltp + i * tickSz, sz: Math.round(40 + Math.random() * 900), n: Math.round(1 + Math.random() * 30) });
  }
  return { bids, asks, tickSz };
}

// time & sales tape entries
function tTape(sym, ltp) {
  const side = Math.random() > 0.5 ? 'B' : 'S';
  return {
    id: Math.random().toString(36).slice(2),
    sym, side, px: ltp + (Math.random() - 0.5) * ltp * 0.001,
    qty: Math.round(5 + Math.random() * 500),
    t: new Date().toLocaleTimeString('en-IN', { hour12: false }),
  };
}

Object.assign(window, {
  TIcon, tcx, fmt, fmt0, sgn, compact,
  TSEED, tSeed, tStep, tCandles, tEma, tDepth, tTape,
});

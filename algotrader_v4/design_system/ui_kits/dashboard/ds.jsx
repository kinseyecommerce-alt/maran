/* ============================================================
   AlgoTrader Pro UI Kit — shared primitives, icons, mock data
   Faithful to algotrader_v4/frontend/src/components/ui/index.tsx
   ============================================================ */

// ---------- Inline Lucide icons (2px stroke line set) ----------
const ICON_PATHS = {
  activity:     <path d="M22 12h-4l-3 9L9 3l-3 9H2" />,
  'trending-up':   <><polyline points="22 7 13.5 15.5 8.5 10.5 2 17" /><polyline points="16 7 22 7 22 13" /></>,
  'trending-down': <><polyline points="22 17 13.5 8.5 8.5 13.5 2 7" /><polyline points="16 17 22 17 22 11" /></>,
  cart:         <><circle cx="8" cy="21" r="1" /><circle cx="19" cy="21" r="1" /><path d="M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12" /></>,
  zap:          <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />,
  square:       <rect x="5" y="5" width="14" height="14" rx="2" />,
  wifi:         <><path d="M5 13a10 10 0 0 1 14 0" /><path d="M8.5 16.5a5 5 0 0 1 7 0" /><path d="M2 8.82a15 15 0 0 1 20 0" /><line x1="12" y1="20" x2="12.01" y2="20" /></>,
  'wifi-off':   <><path d="M2 2l20 20" /><path d="M8.5 16.5a5 5 0 0 1 7 0" /><path d="M2 8.82a15 15 0 0 1 4.17-2.65" /><path d="M10.66 5c4.01-.36 8.14.9 11.34 3.76" /><line x1="12" y1="20" x2="12.01" y2="20" /></>,
  settings:     <><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /><circle cx="12" cy="12" r="3" /></>,
  shield:       <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10" />,
  'alert-triangle': <><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></>,
  eye:          <><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" /><circle cx="12" cy="12" r="3" /></>,
  x:            <><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>,
};

function Icon({ name, size = 16, className = '', style }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      className={className} style={style}>
      {ICON_PATHS[name]}
    </svg>
  );
}

// ---------- formatting ----------
const inr = (v, dp = 2) =>
  '₹' + Number(v).toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp });
const inr0 = (v) => '₹' + Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });

// ---------- primitives ----------
const cx = (...a) => a.filter(Boolean).join(' ');

function Badge({ children, variant = 'neutral' }) {
  const cls = {
    buy: 'bg-green-100 text-green-800', sell: 'bg-red-100 text-red-800',
    neutral: 'bg-slate-100 text-slate-700', warning: 'bg-amber-100 text-amber-800',
    paper: 'bg-amber-100 text-amber-800', live: 'bg-green-100 text-green-800',
  }[variant];
  return <span className={cx('inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold', cls)}>{children}</span>;
}

function Btn({ children, onClick, variant = 'primary', size = 'md', disabled, className, type = 'button' }) {
  const variants = {
    primary: 'bg-indigo-600 hover:bg-indigo-700 text-white',
    buy: 'bg-green-600 hover:bg-green-700 text-white',
    sell: 'bg-red-600 hover:bg-red-700 text-white',
    danger: 'bg-red-600 hover:bg-red-700 text-white',
    ghost: 'bg-transparent hover:bg-slate-100 text-slate-700',
    outline: 'border border-slate-300 hover:bg-slate-50 text-slate-700',
  }[variant];
  const sizes = { sm: 'px-3 py-1 text-sm', md: 'px-4 py-2 text-sm', lg: 'px-6 py-3 text-base' }[size];
  return (
    <button type={type} onClick={onClick} disabled={disabled}
      className={cx('rounded-lg font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-1',
        variants, sizes, disabled && 'opacity-50 cursor-not-allowed', className)}>
      {children}
    </button>
  );
}

function Card({ children, className }) {
  return <div className={cx('bg-white rounded-xl border border-slate-200 shadow-sm', className)}>{children}</div>;
}

function Input(props) {
  return <input {...props} className={cx('w-full border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent placeholder:text-slate-400 bg-white', props.className)} />;
}

function Select({ children, ...props }) {
  return <select {...props} className={cx('w-full border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 bg-white cursor-pointer', props.className)}>{children}</select>;
}

function Pnl({ value }) {
  return <span className={cx('font-mono text-sm font-medium', value >= 0 ? 'text-green-600' : 'text-red-600')}>
    {value >= 0 ? '+' : ''}{inr(value)}
  </span>;
}

function Modal({ open, onClose, title, children }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl p-6 w-full max-w-md mx-4">
        <h2 className="text-lg font-semibold text-slate-900 mb-4">{title}</h2>
        {children}
      </div>
    </div>
  );
}

// ---------- mock market data ----------
const SEED = [
  { sym: 'RELIANCE',  px: 2847.50, rsi: 58, trend: 'UP' },
  { sym: 'TCS',       px: 3912.00, rsi: 42, trend: 'DOWN' },
  { sym: 'HDFCBANK',  px: 1678.30, rsi: 61, trend: 'UP' },
  { sym: 'INFY',      px: 1543.75, rsi: 47, trend: 'NEUTRAL' },
  { sym: 'ICICIBANK', px: 1187.90, rsi: 66, trend: 'UP' },
  { sym: 'SBIN',      px:  812.45, rsi: 38, trend: 'DOWN' },
  { sym: 'BHARTIARTL',px: 1456.20, rsi: 55, trend: 'UP' },
  { sym: 'ITC',       px:  438.60, rsi: 49, trend: 'NEUTRAL' },
];

function seedTicks() {
  const t = {};
  for (const s of SEED) {
    const chg = (Math.random() * 4 - 2);
    const spark = Array.from({ length: 20 }, (_, i) => s.px * (1 + (Math.random() - 0.5) * 0.01 + (chg / 100) * (i / 20)));
    t[s.sym] = {
      symbol: s.sym, ltp: s.px, prev: s.px, change_pct: chg, rsi: s.rsi, trend: s.trend,
      day_high: s.px * 1.012, day_low: s.px * 0.988, spark,
    };
  }
  return t;
}

// random-walk one step
function stepTicks(prev) {
  const next = {};
  for (const k of Object.keys(prev)) {
    const t = prev[k];
    const drift = (Math.random() - 0.5) * t.ltp * 0.0008;
    const ltp = Math.max(1, t.ltp + drift);
    const change_pct = t.change_pct + drift / t.ltp * 100;
    const spark = [...t.spark.slice(1), ltp];
    next[k] = { ...t, prev: t.ltp, ltp, change_pct,
      day_high: Math.max(t.day_high, ltp), day_low: Math.min(t.day_low, ltp), spark };
  }
  return next;
}

Object.assign(window, {
  Icon, inr, inr0, cx, Badge, Btn, Card, Input, Select, Pnl, Modal,
  SEED, seedTicks, stepTicks,
});

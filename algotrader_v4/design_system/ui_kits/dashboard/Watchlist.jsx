/* Watchlist — live symbol rows with sparkline, %chg, trend chip, RSI */
function Sparkline({ data, up }) {
  const w = 64, h = 32;
  const min = Math.min(...data), max = Math.max(...data);
  const rng = max - min || 1;
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * w},${h - ((v - min) / rng) * h}`).join(' ');
  return (
    <svg width={w} height={h} className="overflow-visible">
      <polyline points={pts} fill="none" stroke={up ? '#16A34A' : '#DC2626'} strokeWidth="1.5"
        strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function Watchlist({ ticks, selected, onSelect }) {
  const symbols = Object.keys(ticks).sort();
  return (
    <div className="overflow-y-auto h-full">
      {symbols.map(sym => {
        const t = ticks[sym];
        const up = t.change_pct >= 0;
        const sel = sym === selected;
        return (
          <button key={sym} onClick={() => onSelect(sym)}
            className={cx('w-full flex flex-col px-3 py-2.5 border-b border-slate-100 transition-colors text-left',
              sel ? 'bg-indigo-50 border-l-2 border-l-indigo-500' : 'hover:bg-slate-50')}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5 text-slate-900">
                <Icon name={up ? 'trending-up' : 'trending-down'} size={12}
                  className={up ? 'text-green-500' : 'text-red-500'} />
                <span className="text-xs font-semibold">{sym}</span>
              </div>
              <span className={cx('text-xs font-mono font-semibold tabular-nums', up ? 'text-green-600' : 'text-red-600')}>
                {up ? '+' : ''}{t.change_pct.toFixed(2)}%
              </span>
            </div>
            <div className="flex items-end justify-between mt-1">
              <div>
                <div className="text-sm font-bold font-mono text-slate-900 tabular-nums">{inr(t.ltp)}</div>
                <div className="text-xs text-slate-400 mt-0.5">H: {t.day_high.toFixed(1)} L: {t.day_low.toFixed(1)}</div>
              </div>
              <div className="w-16 h-8"><Sparkline data={t.spark} up={up} /></div>
            </div>
            <div className="flex gap-2 mt-1 items-center">
              <span className={cx('text-xs px-1.5 rounded font-medium',
                t.trend === 'UP' ? 'bg-green-100 text-green-700' :
                t.trend === 'DOWN' ? 'bg-red-100 text-red-700' : 'bg-slate-100 text-slate-600')}>{t.trend}</span>
              <span className="text-xs text-slate-400 font-mono">RSI {t.rsi.toFixed(0)}</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
window.Watchlist = Watchlist;

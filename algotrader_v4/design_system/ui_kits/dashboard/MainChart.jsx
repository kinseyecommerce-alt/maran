/* MainChart — candlestick chart w/ EMA9/21 overlays (cosmetic, SVG) */
function genCandles(base, n = 60) {
  const out = [];
  let px = base * 0.985;
  for (let i = 0; i < n; i++) {
    const o = px;
    const drift = (Math.random() - 0.48) * base * 0.004;
    const c = Math.max(1, o + drift);
    const hi = Math.max(o, c) + Math.random() * base * 0.002;
    const lo = Math.min(o, c) - Math.random() * base * 0.002;
    out.push({ o, c, hi, lo });
    px = c;
  }
  return out;
}
function ema(vals, p) {
  const k = 2 / (p + 1); let e = vals[0]; const out = [e];
  for (let i = 1; i < vals.length; i++) { e = vals[i] * k + e * (1 - k); out.push(e); }
  return out;
}

function MainChart({ symbol, tick }) {
  const candles = React.useMemo(() => genCandles(tick ? tick.ltp : 1000), [symbol]);
  if (!tick) return <div className="flex items-center justify-center h-full text-slate-400 text-sm">Select a symbol</div>;

  const W = 900, H = 360, padT = 16, padB = 24, padR = 56;
  const closes = candles.map(c => c.c);
  const e9 = ema(closes, 9), e21 = ema(closes, 21);
  const all = candles.flatMap(c => [c.hi, c.lo]);
  const min = Math.min(...all), max = Math.max(...all), rng = max - min || 1;
  const y = v => padT + (1 - (v - min) / rng) * (H - padT - padB);
  const cw = (W - padR) / candles.length;
  const up = tick.change_pct >= 0;
  const line = (arr, color) => <polyline fill="none" stroke={color} strokeWidth="1.5" opacity="0.9"
    points={arr.map((v, i) => `${i * cw + cw / 2},${y(v)}`).join(' ')} />;

  return (
    <div className="flex flex-col h-full">
      {/* Chart header */}
      <div className="flex items-center gap-4 px-4 py-2.5 border-b border-slate-100 shrink-0">
        <div className="flex items-baseline gap-2">
          <span className="text-base font-bold text-slate-900">{symbol}</span>
          <span className="text-xs text-slate-400">NSE · 1m</span>
        </div>
        <span className={cx('text-lg font-mono font-bold tabular-nums', up ? 'text-green-600' : 'text-red-600')}>{inr(tick.ltp)}</span>
        <span className={cx('text-xs font-mono font-semibold', up ? 'text-green-600' : 'text-red-600')}>
          {up ? '+' : ''}{tick.change_pct.toFixed(2)}%
        </span>
        <div className="flex-1" />
        <div className="flex items-center gap-3 text-xs font-mono">
          <span className="flex items-center gap-1 text-slate-500"><span className="w-3 h-0.5 bg-indigo-500 inline-block"></span>EMA9</span>
          <span className="flex items-center gap-1 text-slate-500"><span className="w-3 h-0.5 bg-amber-500 inline-block"></span>EMA21</span>
        </div>
      </div>
      {/* SVG chart */}
      <div className="flex-1 min-h-0 bg-white">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full h-full">
          {[0.25, 0.5, 0.75].map(f => (
            <line key={f} x1="0" x2={W - padR} y1={padT + f * (H - padT - padB)} y2={padT + f * (H - padT - padB)} stroke="#F1F5F9" strokeWidth="1" />
          ))}
          {candles.map((c, i) => {
            const x = i * cw + cw / 2;
            const green = c.c >= c.o;
            const col = green ? '#16A34A' : '#DC2626';
            return (
              <g key={i}>
                <line x1={x} x2={x} y1={y(c.hi)} y2={y(c.lo)} stroke={col} strokeWidth="1" />
                <rect x={x - cw * 0.3} width={cw * 0.6} y={y(Math.max(c.o, c.c))}
                  height={Math.max(1, Math.abs(y(c.o) - y(c.c)))} fill={col} />
              </g>
            );
          })}
          {line(e9, '#6366F1')}
          {line(e21, '#F59E0B')}
          <line x1="0" x2={W - padR} y1={y(tick.ltp)} y2={y(tick.ltp)} stroke={up ? '#16A34A' : '#DC2626'} strokeWidth="1" strokeDasharray="4 3" opacity="0.7" />
        </svg>
      </div>
    </div>
  );
}
window.MainChart = MainChart;

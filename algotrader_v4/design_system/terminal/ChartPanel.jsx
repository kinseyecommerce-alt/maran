/* ChartPanel — symbol header, stat strip, candlestick + volume + EMA chart */
function Stat({ label, value, tone }) {
  return (
    <div className="stat">
      <span className="stat-l">{label}</span>
      <span className={tcx('stat-v mono', tone)}>{value}</span>
    </div>
  );
}

function ChartPanel({ symbol, tick }) {
  const candles = React.useMemo(() => tCandles(tick ? tick.ltp : 1000), [symbol]);
  const [tf, setTf] = React.useState('5m');
  if (!tick) return <section className="chart"><div className="chart-empty">NO INSTRUMENT</div></section>;

  const up = tick.change_pct >= 0;
  const W = 1000, H = 420, padR = 64, volH = 70, gap = 10;
  const priceH = H - volH - gap;
  const closes = candles.map(c => c.c);
  const e9 = tEma(closes, 9), e21 = tEma(closes, 21);
  const lows = candles.map(c => c.lo), highs = candles.map(c => c.hi);
  const min = Math.min(...lows), max = Math.max(...highs), rng = max - min || 1;
  const pad = rng * 0.06;
  const y = v => ((max + pad - v) / (rng + pad * 2)) * priceH;
  const cw = (W - padR) / candles.length;
  const maxVol = Math.max(...candles.map(c => c.vol));
  const vy = v => priceH + gap + (1 - v / maxVol) * volH;
  const poly = (arr, color) => <polyline fill="none" stroke={color} strokeWidth="1.5" opacity="0.95"
    points={arr.map((v, i) => `${i * cw + cw / 2},${y(v)}`).join(' ')} />;

  const TFS = ['1m', '5m', '15m', '1H', '1D'];

  return (
    <section className="chart">
      {/* symbol header */}
      <div className="chart-head">
        <div className="ch-id">
          <span className="ch-sym">{symbol}</span>
          <span className="ch-ex">NSE</span>
        </div>
        <div className="ch-price">
          <span key={tick.ltp} className={tcx('ch-ltp mono flash', up ? 'pos' : 'neg')}>₹{fmt(tick.ltp)}</span>
          <span className={tcx('ch-chg mono', up ? 'pos' : 'neg')}>
            <TIcon name={up ? 'up' : 'down'} size={13} />{sgn(tick.change_pct)}{tick.change_pct.toFixed(2)}%
          </span>
        </div>
        <div className="ch-tfs">
          {TFS.map(t => <button key={t} className={tcx('tf', tf === t && 'on')} onClick={() => setTf(t)}>{t}</button>)}
        </div>
      </div>

      {/* stat strip */}
      <div className="stat-strip">
        <Stat label="OPEN" value={fmt(tick.open)} />
        <Stat label="HIGH" value={fmt(tick.day_high)} tone="pos" />
        <Stat label="LOW" value={fmt(tick.day_low)} tone="neg" />
        <Stat label="VWAP" value={fmt(tick.vwap)} />
        <Stat label="RSI" value={tick.rsi.toFixed(0)} tone={tick.rsi > 60 ? 'pos' : tick.rsi < 40 ? 'neg' : ''} />
        <Stat label="VOL" value={compact(tick.vol)} />
      </div>

      {/* chart svg */}
      <div className="chart-canvas">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="chart-svg">
          {/* gridlines */}
          {[0.2, 0.4, 0.6, 0.8].map(f => (
            <line key={f} x1="0" x2={W - padR} y1={f * priceH} y2={f * priceH} stroke="var(--grid)" strokeWidth="1" />
          ))}
          {/* price axis labels */}
          {[0, 0.25, 0.5, 0.75, 1].map(f => {
            const val = max + pad - f * (rng + pad * 2);
            return <text key={f} x={W - padR + 7} y={f * priceH + 3} className="axis-lbl">{fmt(val, val > 1000 ? 0 : 1)}</text>;
          })}
          {/* volume */}
          {candles.map((c, i) => (
            <rect key={'v' + i} x={i * cw + cw * 0.18} width={cw * 0.64} y={vy(c.vol)} height={priceH + gap + volH - vy(c.vol)}
              fill={c.c >= c.o ? 'var(--up)' : 'var(--down)'} opacity="0.22" />
          ))}
          {/* candles */}
          {candles.map((c, i) => {
            const x = i * cw + cw / 2;
            const green = c.c >= c.o;
            const col = green ? 'var(--up)' : 'var(--down)';
            return (
              <g key={i}>
                <line x1={x} x2={x} y1={y(c.hi)} y2={y(c.lo)} stroke={col} strokeWidth="1" />
                <rect x={x - cw * 0.3} width={cw * 0.6} y={y(Math.max(c.o, c.c))} height={Math.max(1, Math.abs(y(c.o) - y(c.c)))} fill={col} />
              </g>
            );
          })}
          {poly(e21, 'var(--amber)')}
          {poly(e9, 'var(--accent)')}
          {/* last price line + tag */}
          <line x1="0" x2={W - padR} y1={y(tick.ltp)} y2={y(tick.ltp)} stroke={up ? 'var(--up)' : 'var(--down)'} strokeWidth="1" strokeDasharray="3 3" opacity="0.85" />
          <rect x={W - padR} y={y(tick.ltp) - 9} width={padR} height={18} fill={up ? 'var(--up)' : 'var(--down)'} />
          <text x={W - padR + 6} y={y(tick.ltp) + 4} className="axis-tag">{fmt(tick.ltp, tick.ltp > 1000 ? 0 : 1)}</text>
        </svg>
        <div className="chart-legend">
          <span><i style={{ background: 'var(--accent)' }} />EMA 9</span>
          <span><i style={{ background: 'var(--amber)' }} />EMA 21</span>
        </div>
      </div>
    </section>
  );
}
window.ChartPanel = ChartPanel;

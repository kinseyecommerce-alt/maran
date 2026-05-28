/* OrderRail — order ticket + depth-of-market ladder (signature) */
function DepthLadder({ depth, ltp, onPick }) {
  const maxSz = Math.max(...depth.bids.map(b => b.sz), ...depth.asks.map(a => a.sz));
  const Row = ({ lvl, side }) => {
    const pos = side === 'bid';
    return (
      <button className="dom-row" onClick={() => onPick(lvl.px.toFixed(2))}>
        <div className={tcx('dom-fill', pos ? 'bid' : 'ask')} style={{ width: `${(lvl.sz / maxSz) * 100}%` }} />
        <span className="dom-n mono">{lvl.n}</span>
        <span className={tcx('dom-sz mono', pos ? 'pos' : 'neg')}>{lvl.sz}</span>
        <span className="dom-px mono">{fmt(lvl.px)}</span>
      </button>
    );
  };
  return (
    <div className="dom">
      <div className="dom-head">
        <span>ORD</span><span>QTY</span><span className="r">PRICE</span>
      </div>
      <div className="dom-asks">
        {[...depth.asks].reverse().map((a, i) => <Row key={'a' + i} lvl={a} side="ask" />)}
      </div>
      <div className="dom-mid">
        <span className="mono">{fmt(ltp)}</span>
        <span className="dom-spread mono">SPREAD {depth.tickSz.toFixed(2)}</span>
      </div>
      <div className="dom-bids">
        {depth.bids.map((b, i) => <Row key={'b' + i} lvl={b} side="bid" />)}
      </div>
    </div>
  );
}

function OrderRail({ symbol, tick, depth, onPlace, onPick, priceField, setPriceField }) {
  const [side, setSide] = React.useState('BUY');
  const [qty, setQty] = React.useState('1');
  const [type, setType] = React.useState('MARKET');
  const [product, setProduct] = React.useState('MIS');

  const est = tick ? (parseInt(qty || '0') || 0) * tick.ltp : 0;
  const place = () => {
    const q = parseInt(qty);
    if (!symbol || !q || q <= 0) return;
    onPlace({ symbol, side, qty: q, type, product, price: type !== 'MARKET' && priceField ? parseFloat(priceField) : tick.ltp });
  };

  return (
    <aside className="rail">
      <div className="panel-head">
        <span className="panel-title">ORDER&nbsp;TICKET</span>
        <span className="panel-meta mono">{product}</span>
      </div>

      <div className="ticket">
        <div className="bs-toggle">
          <button className={tcx('bs', side === 'BUY' && 'buy-on')} onClick={() => setSide('BUY')}>BUY</button>
          <button className={tcx('bs', side === 'SELL' && 'sell-on')} onClick={() => setSide('SELL')}>SELL</button>
        </div>

        <div className="fld-row">
          <label className="fld">
            <span>QTY</span>
            <input type="number" min="1" value={qty} onChange={e => setQty(e.target.value)} className="mono" />
          </label>
          <label className="fld">
            <span>PRICE</span>
            <input value={type === 'MARKET' ? 'MKT' : (priceField || (tick ? tick.ltp.toFixed(2) : ''))}
              onChange={e => setPriceField(e.target.value)} disabled={type === 'MARKET'} className="mono" />
          </label>
        </div>

        <div className="seg-row">
          {['MARKET', 'LIMIT', 'SL-M'].map(t => (
            <button key={t} className={tcx('seg', type === t && 'on')} onClick={() => setType(t)}>{t}</button>
          ))}
        </div>
        <div className="seg-row">
          {['MIS', 'CNC', 'NRML'].map(p => (
            <button key={p} className={tcx('seg', product === p && 'on')} onClick={() => setProduct(p)}>{p}</button>
          ))}
        </div>

        <div className="est-row">
          <span>EST. VALUE</span>
          <span className="mono">₹{fmt0(est)}</span>
        </div>

        <button className={tcx('place', side === 'BUY' ? 'buy' : 'sell')} onClick={place} disabled={!symbol}>
          {side} {qty || 0} <span className="place-sym">{symbol}</span>
        </button>
      </div>

      <div className="panel-head sub">
        <span className="panel-title">DEPTH&nbsp;·&nbsp;{symbol}</span>
        <span className="panel-meta mono"><span className="live-dot sm" />L2</span>
      </div>
      {tick && <DepthLadder depth={depth} ltp={tick.ltp} onPick={(p) => { setType('LIMIT'); onPick(p); }} />}
    </aside>
  );
}
window.OrderRail = OrderRail;

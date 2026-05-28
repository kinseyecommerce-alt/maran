/* OrderPanel — BUY/SELL form with qty, order type, product, square-off */
function OrderPanel({ symbol, tick, onPlace, onSquareOff, hasPositions }) {
  const [side, setSide] = React.useState('BUY');
  const [qty, setQty] = React.useState('1');
  const [orderType, setOrderType] = React.useState('MARKET');
  const [product, setProduct] = React.useState('MIS');
  const [price, setPrice] = React.useState('0');
  const [confirmSq, setConfirmSq] = React.useState(false);

  React.useEffect(() => {
    if (tick && orderType === 'LIMIT') setPrice(tick.ltp.toFixed(2));
  }, [orderType, symbol]);

  const est = tick ? (parseInt(qty || '0') || 0) * tick.ltp : 0;

  const place = () => {
    const q = parseInt(qty);
    if (!symbol || !q || q <= 0) return;
    onPlace({ symbol, side, qty: q, orderType, product, price: orderType !== 'MARKET' ? parseFloat(price) : tick.ltp });
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-slate-100">
        <div className="flex items-center gap-2 text-slate-900">
          <span className="text-slate-500"><Icon name="cart" size={16} /></span>
          <span className="text-sm font-semibold">Order Panel</span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Symbol */}
        <div className="text-center">
          {tick ? (
            <div>
              <div className="text-lg font-bold text-slate-900">{symbol}</div>
              <div className={cx('text-2xl font-mono font-bold', tick.change_pct >= 0 ? 'text-green-600' : 'text-red-600')}>{inr(tick.ltp)}</div>
              <div className="text-xs text-slate-500 mt-1">Bid: {(tick.ltp - 0.15).toFixed(2)} · Ask: {(tick.ltp + 0.15).toFixed(2)}</div>
            </div>
          ) : <div className="text-slate-400 text-sm">No symbol selected</div>}
        </div>

        {/* BUY / SELL toggle */}
        <div className="grid grid-cols-2 gap-1 p-1 bg-slate-100 rounded-xl">
          <button onClick={() => setSide('BUY')}
            className={cx('py-2 rounded-lg text-sm font-bold transition-all flex items-center justify-center gap-1.5',
              side === 'BUY' ? 'bg-green-600 text-white shadow-sm' : 'text-slate-600 hover:text-slate-900')}>
            <Icon name="trending-up" size={14} />BUY <span className="text-xs font-normal opacity-70">(B)</span>
          </button>
          <button onClick={() => setSide('SELL')}
            className={cx('py-2 rounded-lg text-sm font-bold transition-all flex items-center justify-center gap-1.5',
              side === 'SELL' ? 'bg-red-600 text-white shadow-sm' : 'text-slate-600 hover:text-slate-900')}>
            <Icon name="trending-down" size={14} />SELL <span className="text-xs font-normal opacity-70">(S)</span>
          </button>
        </div>

        {/* Fields */}
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">Quantity</label>
            <Input type="number" min="1" value={qty} onChange={e => setQty(e.target.value)} placeholder="1" />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Order Type</label>
              <Select value={orderType} onChange={e => setOrderType(e.target.value)}>
                <option>MARKET</option><option>LIMIT</option><option>SL</option><option>SL-M</option>
              </Select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Product</label>
              <Select value={product} onChange={e => setProduct(e.target.value)}>
                <option>MIS</option><option>CNC</option><option>NRML</option>
              </Select>
            </div>
          </div>
          {orderType !== 'MARKET' && (
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Price</label>
              <Input type="number" step="0.05" value={price} onChange={e => setPrice(e.target.value)} />
            </div>
          )}
          {est > 0 && (
            <div className="text-xs text-slate-500 text-center">
              Est. value: <span className="font-mono font-medium text-slate-700">{inr0(est)}</span>
            </div>
          )}
        </div>

        <Btn variant={side === 'BUY' ? 'buy' : 'sell'} size="lg" className="w-full font-bold text-base" onClick={place} disabled={!symbol}>
          {side} {qty || 0} {symbol || '...'}
        </Btn>

        <div className="pt-2 border-t border-slate-100">
          {confirmSq ? (
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-amber-700 text-xs bg-amber-50 rounded-lg p-2">
                <Icon name="alert-triangle" size={16} /> Square off ALL positions at market price?
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Btn variant="danger" size="sm" onClick={() => { onSquareOff(); setConfirmSq(false); }}>Confirm</Btn>
                <Btn variant="outline" size="sm" onClick={() => setConfirmSq(false)}>Cancel</Btn>
              </div>
            </div>
          ) : (
            <Btn variant="outline" size="sm" className="w-full text-red-600 border-red-200 hover:bg-red-50"
              onClick={() => setConfirmSq(true)} disabled={!hasPositions}>
              Square Off All Positions
            </Btn>
          )}
        </div>
      </div>
    </div>
  );
}
window.OrderPanel = OrderPanel;

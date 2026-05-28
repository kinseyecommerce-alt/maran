/* Header — logo, mode badge, market status, IST clock, WS, bot toggle, settings */
function Header({ mode, marketOpen, botRunning, onToggleBot, onOpenSettings }) {
  const [time, setTime] = React.useState(new Date());
  React.useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <header className="h-14 bg-white border-b border-slate-200 flex items-center px-4 gap-4 shrink-0 z-30">
      {/* Logo */}
      <div className="flex items-center gap-2 min-w-[160px]">
        <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center text-white">
          <Icon name="activity" size={16} />
        </div>
        <div>
          <div className="text-sm font-bold text-slate-900 leading-none">AlgoTrader Pro</div>
          <div className="text-xs text-slate-500 leading-none mt-0.5">Nirma Trade v4</div>
        </div>
      </div>

      <Badge variant={mode === 'LIVE' ? 'live' : 'paper'}>{mode}</Badge>

      <span className="text-xs text-slate-500 font-mono hidden sm:block">
        Ticks: <span className="text-slate-700 font-medium">PAPER</span>
      </span>

      <div className="flex items-center gap-1.5">
        <span className={cx('w-2 h-2 rounded-full', marketOpen ? 'bg-green-500 animate-pulse' : 'bg-slate-400')} />
        <span className="text-xs text-slate-600 hidden sm:block">Market {marketOpen ? 'OPEN' : 'CLOSED'}</span>
      </div>

      <div className="flex-1" />

      <span className="font-mono text-sm text-slate-700 tabular-nums hidden md:block">
        {time.toLocaleTimeString('en-IN', { hour12: false })} IST
      </span>

      <div className="flex items-center gap-1 text-green-500">
        <Icon name="wifi" size={16} />
        <span className="text-xs text-slate-500 hidden sm:block">Live</span>
      </div>

      <Btn variant={botRunning ? 'danger' : 'buy'} size="sm" onClick={onToggleBot}>
        <span className="inline-flex items-center gap-1">
          <Icon name={botRunning ? 'square' : 'zap'} size={14} />
          {botRunning ? 'Stop Bot' : 'Start Bot'}
        </span>
      </Btn>

      <button onClick={onOpenSettings}
        className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500 hover:text-slate-700 transition-colors">
        <Icon name="settings" size={16} />
      </button>
    </header>
  );
}
window.Header = Header;

"""Measure tick fan-out cost + decision latency for a NIFTY-options consumer.
Scenario mirrors Fri 9 Oct: 3 subscriber queues stuck full (agents paused by
owner focus but their queues still registered / lagging), 1 live consumer.
Usage: python tick_queue_bench.py [old|new]"""
import asyncio, sys, time, statistics as st, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("ALGOTRADER_TEST", "1")
import tick_engine as T

MODE = sys.argv[1] if len(sys.argv) > 1 else "new"
N_SYM, RATE, SECS = 150, 1500, 6
WORK_MS = float(os.environ.get("WORK_MS", "0.9"))   # consumer cost per snap
SYMS = [f"NIFTY25OCT{24000+50*i}{'CE' if i%2 else 'PE'}" for i in range(N_SYM)]

class Snap:
    __slots__ = ("symbol", "t")
    def __init__(s, sym, t): s.symbol, s.t = sym, t

def mkq(n):
    if MODE == "old":
        return asyncio.Queue(maxsize=n)
    return T.CoalescingQueue(maxsize=n)

async def main():
    te = T.TickEngine.__new__(T.TickEngine)
    te._queue_drop_count = {}
    te._subscribers = {}
    for name in ("agent_intraday", "agent_scalping", "agent_option_scalping_dead"):
        q = mkq(5000)
        for i in range(5000):
            try: q.put_nowait(Snap(SYMS[i % N_SYM], time.monotonic()))
            except asyncio.QueueFull: break
        te._subscribers[name] = q
    live = mkq(5000); te._subscribers["agent_nifty_options"] = live
    lat, processed, stop = [], [0], [False]
    async def consumer():
        while not stop[0] or live.qsize():
            try: s = await asyncio.wait_for(live.get(), 0.5)
            except asyncio.TimeoutError: continue
            age = time.monotonic() - s.t
            if age <= 5.0:                       # base_agent drops snaps >5 s old
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < WORK_MS / 1000: pass
                lat.append(age * 1000); processed[0] += 1
            if stop[0]: break
    c = asyncio.create_task(consumer())
    fan = []
    t_end = time.monotonic() + SECS; i = 0
    while time.monotonic() < t_end:
        for _ in range(RATE // 100):
            snap = Snap(SYMS[i % N_SYM], time.monotonic()); i += 1
            t0 = time.perf_counter()
            for name, q in list(te._subscribers.items()):
                te._fanout(name, q, snap)
            fan.append((time.perf_counter() - t0) * 1e6)
        await asyncio.sleep(0.01)
    stop[0] = True; await asyncio.sleep(0.6); c.cancel()
    q = st.quantiles
    print(f"mode={MODE} ticks={i} fanout_us p50={st.median(fan):.0f} p99={q(fan, n=100)[98]:.0f} "
          f"| consumer processed={processed[0]} latency_ms p50={st.median(lat):.0f} "
          f"p99={q(lat, n=100)[98]:.0f} max={max(lat):.0f} | live qsize_end={live.qsize()} "
          f"drops={sum(te._queue_drop_count.values())}")
asyncio.run(main())

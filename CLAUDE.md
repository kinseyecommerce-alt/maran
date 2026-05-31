# Ruflo — Claude Code Configuration

## Rules

- Do what has been asked; nothing more, nothing less
- NEVER create files unless absolutely necessary — prefer editing existing files
- NEVER create documentation files unless explicitly requested
- NEVER save working files or tests to root — use `/src`, `/tests`, `/docs`, `/config`, `/scripts`
- ALWAYS read a file before editing it
- NEVER commit secrets, credentials, or .env files
- NEVER add a `Co-Authored-By` trailer to user commits unless this project's `.claude/settings.json` has `attribution.commit` set (#2078). The Claude Code Bash tool may suggest one in its default commit-message template — ignore it. `Co-Authored-By` is semantic authorship attribution under git/GitHub convention; the tool is the facilitator, not a co-author.
- Keep files under 500 lines
- Validate input at system boundaries

## Agent Comms (SendMessage-First Coordination)

Named agents coordinate via `SendMessage`, not polling or shared state.

```
Lead (you) ←→ architect ←→ developer ←→ tester ←→ reviewer
              (named agents message each other directly)
```

### Spawning a Coordinated Team

```javascript
// ALL agents in ONE message, each knows WHO to message next
Agent({ prompt: "Research the codebase. SendMessage findings to 'architect'.",
  subagent_type: "researcher", name: "researcher", run_in_background: true })
Agent({ prompt: "Wait for 'researcher'. Design solution. SendMessage to 'coder'.",
  subagent_type: "system-architect", name: "architect", run_in_background: true })
Agent({ prompt: "Wait for 'architect'. Implement it. SendMessage to 'tester'.",
  subagent_type: "coder", name: "coder", run_in_background: true })
Agent({ prompt: "Wait for 'coder'. Write tests. SendMessage results to 'reviewer'.",
  subagent_type: "tester", name: "tester", run_in_background: true })
Agent({ prompt: "Wait for 'tester'. Review code quality and security.",
  subagent_type: "reviewer", name: "reviewer", run_in_background: true })

// Kick off the pipeline
SendMessage({ to: "researcher", summary: "Start", message: "[task context]" })
```

### Patterns

| Pattern | Flow | Use When |
|---------|------|----------|
| **Pipeline** | A → B → C → D | Sequential dependencies (feature dev) |
| **Fan-out** | Lead → A, B, C → Lead | Independent parallel work (research) |
| **Supervisor** | Lead ↔ workers | Ongoing coordination (complex refactor) |

### Rules

- ALWAYS name agents — `name: "role"` makes them addressable
- ALWAYS include comms instructions in prompts — who to message, what to send
- Spawn ALL agents in ONE message with `run_in_background: true`
- After spawning: STOP, tell user what's running, wait for results
- NEVER poll status — agents message back or complete automatically

## Swarm & Routing

### Config
- **Topology**: hierarchical-mesh (anti-drift)
- **Max Agents**: 15
- **Memory**: hybrid
- **HNSW**: Enabled
- **Neural**: Enabled

```bash
npx @claude-flow/cli@latest swarm init --topology hierarchical --max-agents 8 --strategy specialized
```

### Agent Routing

| Task | Agents | Topology |
|------|--------|----------|
| Bug Fix | researcher, coder, tester | hierarchical |
| Feature | architect, coder, tester, reviewer | hierarchical |
| Refactor | architect, coder, reviewer | hierarchical |
| Performance | perf-engineer, coder | hierarchical |
| Security | security-architect, auditor | hierarchical |

### When to Swarm
- **YES**: 3+ files, new features, cross-module refactoring, API changes, security, performance
- **NO**: single file edits, 1-2 line fixes, docs updates, config changes, questions

### 3-Tier Model Routing

| Tier | Handler | Use Cases |
|------|---------|-----------|
| 1 | Agent Booster (WASM) | Simple transforms — skip LLM, use Edit directly |
| 2 | Haiku | Simple tasks, low complexity |
| 3 | Sonnet/Opus | Architecture, security, complex reasoning |

## Memory & Learning

### Before Any Task
```bash
npx @claude-flow/cli@latest memory search --query "[task keywords]" --namespace patterns
npx @claude-flow/cli@latest hooks route --task "[task description]"
```

### After Success
```bash
npx @claude-flow/cli@latest memory store --namespace patterns --key "[name]" --value "[what worked]"
npx @claude-flow/cli@latest hooks post-task --task-id "[id]" --success true --store-results true
```

### MCP Tools (use `ToolSearch("keyword")` to discover)

| Category | Key Tools |
|----------|-----------|
| **Memory** | `memory_store`, `memory_search`, `memory_search_unified` |
| **Bridge** | `memory_import_claude`, `memory_bridge_status` |
| **Swarm** | `swarm_init`, `swarm_status`, `swarm_health` |
| **Agents** | `agent_spawn`, `agent_list`, `agent_status` |
| **Hooks** | `hooks_route`, `hooks_post-task`, `hooks_worker-dispatch` |
| **Security** | `aidefence_scan`, `aidefence_is_safe`, `aidefence_has_pii` |
| **Hive-Mind** | `hive-mind_init`, `hive-mind_consensus`, `hive-mind_spawn` |

### Background Workers

| Worker | When |
|--------|------|
| `audit` | After security changes |
| `optimize` | After performance work |
| `testgaps` | After adding features |
| `map` | Every 5+ file changes |
| `document` | After API changes |

```bash
npx @claude-flow/cli@latest hooks worker dispatch --trigger audit
```

## Agents

**Core**: `coder`, `reviewer`, `tester`, `planner`, `researcher`
**Architecture**: `system-architect`, `backend-dev`, `mobile-dev`
**Security**: `security-architect`, `security-auditor`
**Performance**: `performance-engineer`, `perf-analyzer`
**Coordination**: `hierarchical-coordinator`, `mesh-coordinator`, `adaptive-coordinator`
**GitHub**: `pr-manager`, `code-review-swarm`, `issue-tracker`, `release-manager`

Any string works as a custom agent type.

## Build & Test

- ALWAYS run tests after code changes
- ALWAYS verify build succeeds before committing

```bash
npm run build && npm test
```

## CLI Quick Reference

```bash
npx @claude-flow/cli@latest init --wizard           # Setup
npx @claude-flow/cli@latest swarm init --v3-mode     # Start swarm
npx @claude-flow/cli@latest memory search --query "" # Vector search
npx @claude-flow/cli@latest hooks route --task ""    # Route to agent
npx @claude-flow/cli@latest doctor --fix             # Diagnostics
npx @claude-flow/cli@latest security scan            # Security scan
npx @claude-flow/cli@latest performance benchmark    # Benchmarks
```

26 commands, 140+ subcommands. Use `--help` on any command for details.

## Setup

```bash
claude mcp add claude-flow -- npx -y @claude-flow/cli@latest
npx @claude-flow/cli@latest daemon start
npx @claude-flow/cli@latest doctor --fix
```

**Agent tool** handles execution (agents, files, code, git). **MCP tools** handle coordination (swarm, memory, hooks). **CLI** is the same via Bash.

---

## AlgoTrader Pro v4 — Project Context

### Overview
NSE/BSE algorithmic trading system — 8 strategy agents, Zerodha Kite broker, TrueData
market feed, Claude AI trade gate, FastAPI backend, asyncio runtime.
Working directory: `algotrader_v4/`
Active branch: `claude/loving-bell-0eMf7`
Trading modes: `PAPER` (default, safe) / `LIVE` (real Zerodha orders)

### Full Pipeline
```
TrueData WS (_on_tick) ──► tick_engine._process_tick()   [tick_engine.py:567]
                                  │  indicators + MarketSnapshot
                                  ▼  asyncio.Queue per agent  [tick_engine.py:593]
                         base_agent._run_loop()           [base_agent.py:302]
                           ├─ trailing_sl_engine.on_tick()  [base_agent.py:323]
                           ├─ agent.evaluate_tick(snap)     [base_agent.py:327]
                           ├─ claude_trade_gate.assess()    [base_agent.py:362]
                           └─ _try_enter()                  [base_agent.py:409]
                                 ├─ order_guard.can_place()
                                 ├─ risk_manager.pre_trade_checks()
                                 ├─ sebi_compliance.check()
                                 ├─ kite_client.place_order(MARKET entry)  [:479]
                                 ├─ trailing_sl_engine.register()          [:528]
                                 └─ kite_client.place_order(SL-M)          [:539]
```

### Strategy Agents
| Agent | Symbol | Signal Logic | File |
|-------|--------|-------------|------|
| intraday | RELIANCE, HDFC… | EMA9/21 + RSI + VWAP + volume | agents/intraday_agent.py |
| scalping | TCS, INFY… | EMA9 crossover (2-tick) | agents/scalping_agent.py |
| swing | HDFCBANK… | EMA50/200 weekly trend | agents/swing_agent.py |
| options | NIFTY, INFY… | EMA cross → CE/PE contract | agents/options_agent.py |
| futures | SBIN, TATASTEEL… | EMA trend + MACD accel | agents/futures_agent.py |

### Key Files
```
algotrader_v4/
├── main.py                  FastAPI app, all REST endpoints
├── tick_engine.py           Market data ingestion + indicators
├── agents/
│   ├── base_agent.py        Shared async loop (_run_loop, _try_enter, _on_sl_hit)
│   ├── intraday_agent.py
│   ├── scalping_agent.py
│   ├── swing_agent.py
│   ├── options_agent.py
│   └── futures_agent.py
├── kite_client.py           Zerodha broker — LIVE + PAPER modes
├── risk_manager.py          Position sizing, daily loss, Kelly, ATR sizing
├── order_guard.py           Duplicate/overtrade/cooldown gate
├── trailing_sl_engine.py    TSL + target management per strategy
├── sebi_compliance.py       Regulatory checks
├── claude_trade_gate.py     Claude AI veto layer
├── master_agent_v5.py       Regime detection + agent orchestration
├── config.py / settings.py  All tuneable parameters (Settings dataclass)
├── market_data.py           TrueData + yfinance + CSV cache
├── backtest_engine.py       Walk-forward + strategy comparison
├── atomic_bracket.py        Bracket order management
└── nse_day_simulation.py    Offline GBM simulation, all 5 agents
```

### Tests (MUST pass after every change)
```bash
cd algotrader_v4
python test_full_pipeline.py    # 30/30  — all 5 agents: ingestion→order→exit
python test_pipeline.py         # 345/345 — cross-module risk/guard/SEBI/kite/TSL + all phases
python test_sim_orders_flow.py  # 13/13  — PAPER order/guard/risk flow
```

### Security Constraints (NEVER violate)
- `.env` is gitignored — credentials NEVER committed
- All secrets (KITE_API_KEY, KITE_API_SECRET, KITE_TOTP_SECRET, ANTHROPIC_API_KEY,
  JWT_SECRET_KEY, TRUEDATA_USER/PASSWORD) live in `.env` ONLY
- `Read(./.env)` is denied in `.claude/settings.json` — do not circumvent

### Completed Roadmap (all phases done ✅)
```
Phase 6  — FuturesAgent TSL config + test coverage             ✅ trailing_sl_engine.py
Phase 1A — Transaction cost engine (Zerodha: STT/GST/stamp)    ✅ risk_manager.py
Phase 1B — ATR-proportional slippage model (PAPER mode)        ✅ atomic_bracket.py
Phase 1C — Kelly criterion wired to adaptive stats             ✅ risk_manager.py
Phase 2A — Walk-forward: 730 days, 12 folds, oos_sharpe        ✅ backtest_engine.py
Phase 2B — Monte Carlo 1000x: sharpe_pct + dd_95pct            ✅ backtest_engine.py
Phase 3  — Regime hysteresis, VIX z-score, sector limits       ✅ master_agent_v5.py / risk_manager.py
Phase 4  — SQLite persistence with PostgreSQL/Redis fallback   ✅ state_store.py
Phase 5  — Options chain UI, drawdown chart, trade journal,    ✅ static/dashboard.html
           multi-leg builder, keyboard shortcuts B/K/R/1-5
FuturesAgent Intelligence — 12 patterns, 9-factor ctx_bonus,   ✅ agents/strategy_agents.py
  macro gate, L2 wall gate, rollover awareness
1000-Trader Upgrade — signal_aggregator (consensus +50% qty),  ✅ signal_aggregator.py
  PairsAgent (statarb), conviction sizing, 100+ symbol pairs      agents/strategy_agents.py
  scanner criteria for all 8 agents (15→100 symbols/agent)        symbol_scanner.py
Macro signals (USD/INR, crude, S&P, VIX) → risk gate           ✅ macro_signals.py
FII/DII institutional flow → ±20% qty sizing                   ✅ alt_data.py / risk_manager.py
L2 order book depth → wall detection per symbol                ✅ tick_engine.py
Latency: async order placement, positions cache 2s TTL         ✅ base_agent.py / kite_client.py
```

### PAPER Mode Behaviour
- Orders stored in `kite_client._paper_orders` as dicts
- Tagged: `Agent-{strategy}` (entry), `Agent-{strategy}-SL` (stop), `TSL-HIT-{strategy}` (exit)
- OptionsAgent places orders for contract symbols (e.g. `INFY2606041650CE`), not the underlying
- TSL positions keyed by underlying symbol (`pos.symbol = snap.symbol`)

### Build & Test (override generic section above)
```bash
cd algotrader_v4 && python test_full_pipeline.py   # primary smoke test
```

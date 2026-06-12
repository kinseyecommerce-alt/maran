#!/usr/bin/env python3
"""
Fable 5 Full-Application Audit — AlgoTrader Pro v4
====================================================
Run this on any machine where ANTHROPIC_API_KEY is set.
Packs the 15 core trading-path modules into a single Fable 5 call
and writes a prioritized enhancement report to logs/fable_audit_report.md.

Usage:
    python scripts/fable_audit.py              # full audit (~$2 at current pricing)
    python scripts/fable_audit.py --dry-run    # estimate cost only, no API call
    python scripts/fable_audit.py --full       # include every .py file (~200K tokens)
    python scripts/fable_audit.py --symbols "risk_manager.py,kite_client.py"  # custom files
"""
import argparse
import os
import sys
import json
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent          # algotrader_v4/
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

# ── Short audit prompt (Deliverable 1) ────────────────────────────────────────
AUDIT_PROMPT = """
You are auditing AlgoTrader Pro v4, a production NSE/BSE algorithmic trading
system (~40,000 lines Python). It runs 8 strategy agents, routes through Claude
AI trade gate, and places real Zerodha Kite orders.

Audit the provided codebase across these dimensions and return a prioritized
enhancement table:

1. ORDER SAFETY — double orders, orphaned SL-M, race conditions in claim/lock
2. ASYNC CORRECTNESS — blocking calls on event loop (time.sleep, sync I/O)
3. RISK MANAGEMENT — daily-loss race, Kelly edge cases, position count bypass
4. ERROR HANDLING — broker 503/429 timeouts, malformed Claude JSON, feed failures
5. SECURITY — unauthenticated endpoints, CORS, kill-switch persistence
6. LATENCY — hot-path bottlenecks, indicator staleness, queue backpressure
7. ARCHITECTURE DEBT — circular imports, unbounded state growth, missing fallbacks

For each finding return:
  PRIORITY (P0/P1/P2)  |  FILE:LINE  |  ISSUE  |  CONCRETE FIX (1-3 sentences)

Group by priority. P0 = can lose money or corrupt state today.
P1 = serious risk in LIVE mode. P2 = reliability / observability gap.

Be precise: cite actual line numbers from the code below, not approximations.
""".strip()

# ── Core trading-path modules to pack ─────────────────────────────────────────
CORE_MODULES = [
    "agents/base_agent.py",
    "agents/strategy_agents.py",
    "kite_client.py",
    "risk_manager.py",
    "order_guard.py",
    "trailing_sl_engine.py",
    "tick_engine.py",
    "master_agent_v5.py",
    "claude_trade_gate.py",
    "atomic_bracket.py",
    "sebi_compliance.py",
    "state_store.py",
    "broker_router.py",
    "config.py",
    "main.py",
]


def _load_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        try:
            from dotenv import dotenv_values
            env = dotenv_values(ROOT / ".env")
            key = env.get("ANTHROPIC_API_KEY", "")
        except ImportError:
            pass
    if not key:
        sys.exit("ERROR: ANTHROPIC_API_KEY not found in environment or .env file")
    return key


def _pack_context(files: list[str]) -> str:
    parts = []
    for rel in files:
        path = ROOT / rel
        if not path.exists():
            print(f"  [skip] {rel} — not found", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"### FILE: {rel}\n```python\n{text}\n```")
    return "\n\n".join(parts)


def _estimate_tokens(text: str) -> int:
    return len(text) // 4   # rough 4-chars-per-token estimate


def _cost_estimate(input_tok: int, output_tok: int = 10_000) -> float:
    # Fable 5: $10/MTok input, $50/MTok output
    return (input_tok * 10 + output_tok * 50) / 1_000_000


def run_audit(files: list[str], dry_run: bool) -> None:
    print("Packing codebase context…")
    code_context = _pack_context(files)
    full_prompt = AUDIT_PROMPT + "\n\n" + code_context

    in_tok = _estimate_tokens(full_prompt)
    out_tok = 15_000
    cost = _cost_estimate(in_tok, out_tok)
    print(f"  Input  ~{in_tok:,} tokens")
    print(f"  Output ~{out_tok:,} tokens (target)")
    print(f"  Cost   ~${cost:.2f} (Fable 5 pricing)")

    if dry_run:
        print("\n[dry-run] Skipping API call. Prompt size verified.")
        (LOGS / "fable_audit_prompt.txt").write_text(full_prompt, encoding="utf-8")
        print(f"  Prompt written to: logs/fable_audit_prompt.txt")
        return

    key = _load_key()

    try:
        import anthropic
    except ImportError:
        sys.exit("ERROR: anthropic package not installed — run: pip install anthropic")

    client = anthropic.Anthropic(api_key=key)

    print("\nCalling claude-fable-5 (streaming, effort=high)…")
    report_chunks: list[str] = []
    t0 = time.monotonic()

    with client.messages.stream(
        model="claude-fable-5",
        max_tokens=32_000,
        output_config={"effort": "high"},
        system=[{
            "type": "text",
            "text": "You are a senior systems engineer and security researcher specialising "
                    "in financial software. Return only the prioritized audit table — "
                    "no preamble, no summary paragraph, no markdown headers beyond the table.",
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": full_prompt}],
    ) as stream:
        for text in stream.text_stream:
            report_chunks.append(text)
            print(text, end="", flush=True)

    elapsed = time.monotonic() - t0
    report = "".join(report_chunks)

    # check refusal
    final_msg = stream.get_final_message()
    if final_msg.stop_reason == "refusal":
        sys.exit("\n\nFable 5 returned a refusal. Check model policy or prompt content.")

    out_file = LOGS / "fable_audit_report.md"
    header = (
        f"# AlgoTrader Pro v4 — Fable 5 Audit Report\n"
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Model: claude-fable-5  |  Effort: high  |  "
        f"Elapsed: {elapsed:.0f}s\n\n"
    )
    out_file.write_text(header + report, encoding="utf-8")
    print(f"\n\nReport written to: {out_file}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fable 5 AlgoTrader audit runner")
    ap.add_argument("--dry-run", action="store_true",
                    help="Estimate cost and pack prompt without calling the API")
    ap.add_argument("--full", action="store_true",
                    help="Include every .py file in the project")
    ap.add_argument("--symbols", default="",
                    help="Comma-separated list of files to include (overrides default)")
    args = ap.parse_args()

    if args.symbols:
        files = [f.strip() for f in args.symbols.split(",") if f.strip()]
    elif args.full:
        files = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                       if ".venv" not in str(p) and "__pycache__" not in str(p))
    else:
        files = CORE_MODULES

    print(f"AlgoTrader Pro v4 — Fable 5 Audit")
    print(f"Files: {len(files)}  |  Mode: {'dry-run' if args.dry_run else 'LIVE'}")
    print()
    run_audit(files, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

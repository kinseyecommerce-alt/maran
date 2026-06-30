---
name: .env overrides pydantic defaults
description: algotrader_v4/.env has hardcoded values that override config.py BaseSettings defaults — always update both.
---

## Rule
When changing any `config.py` default, **also update `algotrader_v4/.env`** — pydantic_settings reads `.env` and its values override Python-level defaults.

**Why:** The project has `algotrader_v4/.env` with 100+ explicit key=value pairs. pydantic-settings loads this file and its values take priority over the field defaults written in the Python class body. A `grep` on `config.py` showing the new value is not enough — the running process will still use the `.env` value until that is updated too.

**How to apply:**
- After editing any setting in `config.py`, run `grep -i <field_name> algotrader_v4/.env` to check for an override.
- If found, `sed -i` the `.env` file to match the new value, then restart the workflow.
- Verify with `cd algotrader_v4 && python -c "from config import settings; print(settings.<field>)"`.

**Known stale overrides caught:**
- `USE_CLAUDE_TRADE_GATE=false` (was disabling the gate entirely)
- `CLAUDE_GATE_MODEL=claude-sonnet-4-6` (was using old model)
- `USE_EXTENDED_THINKING=false`, `GATE_THINKING_BUDGET=2000`, `GATE_API_TIMEOUT=5.0`
- `ANTHROPIC_API_KEY=` (empty line — shadowing Replit secret; removed)

---
name: Agent last_signal is a dict not a string
description: AgentState.last_signal is typed as dict in Python, defaults to {}, causes a React render crash if used with ||
---

## The Rule

Never render `agent.last_signal` directly in JSX using `||` as a fallback. Always type-check first.

**Why:** `AgentState.last_signal` in `base_agent.py` line 329 is `dict = field(default_factory=dict)`. Agents that haven't fired any signal return `{}` (empty dict). In React, `{} || '—'` evaluates to `{}` (truthy), and React throws "Objects are not valid as a React child (found: object with keys {})".

**How to apply:**

When rendering `last_signal` in any component:
```tsx
const ls = agent?.last_signal as unknown
let display = '—'
if (typeof ls === 'string' && ls) display = ls
else if (ls && typeof ls === 'object') {
  const s = ls as Record<string, unknown>
  display = [s.symbol, s.action].filter(Boolean).join(' ') || '—'
}
```

The signal dict shape (when populated) includes: `symbol`, `action`, `price`, `score`, `pattern`, `gate_conf`, `gate_reason`, `sl`, `target`.

**General rule:** Any Python field typed as `dict` or `Optional[dict]` will arrive in JS as an object or `{}`. Never use `someApiValue || 'fallback'` as a React child without checking `typeof`.

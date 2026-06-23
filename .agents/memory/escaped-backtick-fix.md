---
name: Escaped Backtick Fix
description: DESIGN subagents emit escaped backticks in .tsx files — always fix before use.
---

DESIGN subagents consistently write `\`` and `\${` in generated .tsx files (escaped template literals).
These cause syntax errors in TypeScript/JSX.

Fix commands (run after any subagent-generated .tsx file):
```bash
sed -i 's/\\`/`/g' path/to/file.tsx
sed -i 's/\\\${/${/g' path/to/file.tsx
```

**Why:** The sandbox environment the DESIGN subagent runs in escapes backticks in JSX string literals.
**How to apply:** Always run these two sed commands immediately after creating or editing any .tsx file via a DESIGN subagent.

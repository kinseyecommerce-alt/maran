---
name: algotrader-pro-design
description: Use this skill to generate well-branded interfaces and assets for AlgoTrader Pro (Nirma Trade), the algorithmic trading dashboard for NSE/BSE Indian equity markets — either for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping.
user-invocable: true
---

Read the README.md file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

## Quick reference
- **Product:** AlgoTrader Pro / Nirma Trade v4 — light-theme algo-trading cockpit for NSE/BSE.
- **Brand color:** Indigo-600 `#4F46E5`. **Trade semantics:** green-600 `#16A34A` (buy/gain) / red-600 `#DC2626` (sell/loss). **Neutrals:** slate scale. **Caution:** amber.
- **Type:** Inter (UI) + JetBrains Mono (all numbers, `tabular-nums`). ₹ + Indian digit grouping, IST time.
- **Icons:** Lucide (2px stroke line) + functional emoji for tabs/agents/alerts.
- **Vibe:** dense, fast, numbers-first financial terminal. Flat fills, hairline slate borders, `rounded-xl` white cards, restrained shadows, minimal/functional motion. No gradients, no imagery, no decoration.

## Files
- `README.md` — full product context, content & visual foundations, iconography, index.
- `colors_and_type.css` — import for all color + type tokens (base vars + semantic classes).
- `assets/` — logo (mark + wordmark).
- `preview/` — specimen cards (colors, type, spacing, components, brand).
- `ui_kits/dashboard/` — interactive, pixel-accurate dashboard recreation + reusable JSX components.

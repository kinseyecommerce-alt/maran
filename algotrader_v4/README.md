

## Strategy inventor (trend-driven, PAPER first)

Each segment agent can propose short-lived strategies from the market regime
(trend / volatility / breadth). Toggle **Invent ON** in the Invented tab
(or `POST /invent/enabled {"enabled":true}`).

- **PAPER:** invented strategies place paper orders only (tag `INVENTED-<id>`,
  SIMULATED when prices are sim). Caps: invent cooldown, max concurrent per
  segment / global. Segment kill switch expires them.
- **LIVE tiny (never auto):** only when global LIVE + segment armed with typed
  `SEND`, Kite API key/secret + daily login present, paper warm-up passed
  (N fills or M minutes), and `POST /invent/{id}/arm-live-tiny` with
  `confirm=true` + `confirm_text="SEND"`. Quantity = 1 share / 1 lot.
- Without Kite, invent+paper still works on the simulator / NSE public feed.
  LIVE tiny needs Kite credentials — see `/invent/status` → `live_tiny_requirements`.

Env: `SEGMENT_PAPER_AFTER_HOURS=true` lets every segment invent overnight in PAPER.

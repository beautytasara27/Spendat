# ADR-0010: Default caps and thresholds

**Status:** accepted · 2026-09-29

| Setting | Default | Rationale |
|---|---|---|
| Per-transaction policy cap | $50 | routine small purchases autonomous; anything bigger reviewed |
| Category caps | electronics accessories $30, books $40, office $75* | tighter where prices are predictable (*effective limit is min with per-transaction) |
| Daily / weekly policy caps | $150 / $400 | a few routine buys a day |
| Price anomaly | ±40% vs rolling 30-day average, after 3 purchases | catches price spikes and suspiciously cheap listings |
| Velocity | > 5 purchases per 60 min | runaway loops, duplicated requests |
| Approval timeout | 60 min | escalations never stall indefinitely |
| **Hard** per-transaction / daily / weekly | $200 / $300 / $750 | circuit breakers above any policy setting; env-only |
| SPT lifetime | 15 min | long enough for checkout, short enough to be useless if leaked |

All policy values are YAML; hard limits are environment variables so a policy edit cannot
raise them.

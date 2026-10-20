# 100-review cost and runtime calculator

| Mode | Command | Paid? | Needs keys? |
|---|---|---|---|
| **Offline replay (default)**: recompute every number from saved usage and editable rates | `python cost/calculator.py` | no | no |
| Change assumptions (rates, budget, workers, output cap, fallback fraction) | `python cost/calculator.py --rates my_rates.csv --budget 6 --workers 4 --output-token-cap 2000 --fallback-cap 0.05` | no | no |
| Refresh projections from the larger runs and the full run's actuals | `python cost/calculator.py --calibrate runs/dev500 runs/dev10k runs/full` | no | no |
| Grader self-test: 2x rates → 2x API cost with the same measured time; projected volume never changes measured results | `python cost/calculator.py selftest` | no | no |
| **Explicit paid pilot**: cold run on `data/raw/cost_100.csv` (checksum verified) with an empty result cache and 1 worker, all stages; then a warm rerun on saved results | `python cost/calculator.py pilot --confirm-paid` | yes (≈ $0.04) | yes |

Importing or opening `calculator.py` makes no API calls. The pilot refuses to run without `--confirm-paid`.

## Files
- [report.md](report.md): the measured-100 tables (per stage: provider, model, tier, settings, prompt version, batch size, workers,
  requests/attempts/failures, usage, cost), billing arithmetic per item, cold vs. warm wall-clock, unit metrics, full-run base and
  conservative scenarios (with and without exact-text reuse), budget flags, the batching/caching explainer and console reconciliation.
- [pilot_records.jsonl](pilot_records.jsonl): one record per pilot ID (row hash + common labels). [pilot_calls.jsonl](pilot_calls.jsonl): every
  attempted call, cold and warm, with request IDs, settings, usage and latency.
- [rates.csv](rates.csv): editable prices in billing units, with the tier, dated source link and notes. [usage.csv](usage.csv): one row per call ×
  billing item (`billed_units × usd_per_unit = item_cost_usd`), with charge status (measured / not billed / unknown on timeout).
- [measurements.json](measurements.json): pilot wall-clock, statuses and warm-run call counts. [projection.json](projection.json): unit rates + scenarios.
- [reconciliation.json](reconciliation.json): logged spend vs. provider consoles (match: Anthropic $3.84, TypeSafe $0.02).
- [attempt1_close_bug/](attempt1_close_bug/): the first real pilot, which exited with code 1 at shutdown (kept as failure evidence).

API spend is shown separately from local compute, which is reported as **unknown** (not metered), never as $0.

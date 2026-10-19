# 100-review cost and runtime calculator

Generated offline from `cost/pilot_calls.jsonl` + `rates.csv` (no API key, no model calls). Pilot run: 2026-10-18 19:57:34 PDT.

## 1. Measured pilot (cost_100.csv, unchanged)

- Input SHA-256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e` (matches manifest: True); 100 source IDs; 100 unique nonempty texts
- **cold**: wall-clock **408.8 s**, workers 1, completed 100, quarantined 0, pending 0, result-cache hits 0, exit code 1
- **warm**: wall-clock **0.1 s**, workers 1, completed 100, quarantined 0, pending 0, result-cache hits 0, exit code 0
- Warm rerun: **0 new enrichment calls**; downstream calls in warm run: 0 (verify/group/memo outputs are cached by input + config hash)
- Declared verification sample: 20 records (seeded hash order); enrichment retry policy: invalid/missing lines retried once, then quarantined

### Per stage (cold run)

| stage | provider | model (resolved) | tier | settings | prompt/schema | reviews/request | workers | requests ok | attempts | failed | review slots | input | cache write | cache read | output | cost | unknown charges |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| enrich | anthropic | claude-haiku-5-5 | batch | `{"effort": "low", "max_tokens": 2000, "thinking": "disabled", "tier": "batch"}` | enrich-codes-v1 | <=50 | 1 | 2 | 2 | 0 | 100 | 4,537 | 3,104 | 3,104 | 1,777 | $0.000881 | 0 |
| verify | typesafe | jev-1.13.0 | n/a | `{"retry": "pipeline", "timeout_s": 30}` | jev-q-v2 | 1 | 1 | 20 | 20 | 0 | 20 | 15,284 | 0 | 0 | 3,927 | $0.000642 | 0 |
| group | anthropic | claude-haiku-5-5 | standard | `{"effort": "low", "max_tokens": 8000, "thinking": "disabled"}` | group-v1 | aggregate pack | 1 | 1 | 1 | 0 | 0 | 5,107 | 701 | 0 | 1,389 | $0.001293 | 0 |
| memo | anthropic | claude-sonnet-5-5 | standard | `{"effort": "medium", "max_tokens": 12000, "thinking": "adaptive"}` | memo-v1 | aggregate pack | 1 | 1 | 1 | 0 | 0 | 6,164 | 1,198 | 0 | 1,944 | $0.0348 | 0 |

**API spend: cold $0.0376, warm (incremental) $0.000000.** Local compute (MacBook, ingestion + validation + SQLite): **unknown, not metered** (not counted as $0).

### Billing arithmetic (cold run): item cost = billed units x price per unit

| rate key | item | billed units | USD per 1M | item cost | price source (checked) |
|---|---|---|---|---|---|
| claude-haiku-5-5 | cache_write_tokens | 701 | 0.1250 | $0.000088 | https://platform.claude.com/docs/en/about-claude/pricing (2026-10-18) |
| claude-haiku-5-5 | input_tokens | 5,107 | 0.1000 | $0.000511 | https://platform.claude.com/docs/en/about-claude/pricing (2026-10-18) |
| claude-haiku-5-5 | output_tokens | 1,389 | 0.5000 | $0.000695 | https://platform.claude.com/docs/en/about-claude/pricing (2026-10-18) |
| claude-haiku-5-5:batch | cache_read_tokens | 3,104 | 0.0050 | $0.000016 | https://platform.claude.com/docs/en/build-with-claude/batch-processing (2026-10-18) |
| claude-haiku-5-5:batch | cache_write_tokens | 3,104 | 0.0625 | $0.000194 | https://platform.claude.com/docs/en/build-with-claude/batch-processing (2026-10-18) |
| claude-haiku-5-5:batch | input_tokens | 4,537 | 0.0500 | $0.000227 | https://platform.claude.com/docs/en/build-with-claude/batch-processing (2026-10-18) |
| claude-haiku-5-5:batch | output_tokens | 1,777 | 0.2500 | $0.000444 | https://platform.claude.com/docs/en/build-with-claude/batch-processing (2026-10-18) |
| claude-sonnet-5-5 | cache_write_tokens | 1,198 | 2.5000 | $0.002995 | https://platform.claude.com/docs/en/about-claude/pricing (2026-10-18) |
| claude-sonnet-5-5 | input_tokens | 6,164 | 2.0000 | $0.0123 | https://platform.claude.com/docs/en/about-claude/pricing (2026-10-18) |
| claude-sonnet-5-5 | output_tokens | 1,944 | 10.0000 | $0.0194 | https://platform.claude.com/docs/en/about-claude/pricing (2026-10-18) |
| jev-latest | input_tokens | 15,284 | 0.0420 | $0.000642 | https://www.eesel.ai/blog/typesafe-jev-pricing (TypeSafe homepage price $42 per billion input tokens; no official pricing page found) (2026-10-18) |
| jev-latest | output_tokens | 3,927 | 0.0000 | $0.000000 | https://www.eesel.ai/blog/typesafe-jev-pricing (2026-10-18) |

### Unit metrics

- Cost per 1,000 input rows: **$0.3758**; per completed record: $0.000376
- Enrichment only: $0.000009 per distinct text (45.4 input + 17.8 output tokens per review; 3104 cached system-prompt tokens per request); same usage at standard tier would be $0.000018
- Throughput: cold 0.24 records/s end to end (dominated by Message Batch turnaround 383 s); warm 714.3 records/s
- Stage seconds (cold): {"ingest": 0.0, "enrich": 384.1, "verify": 2.47, "group": 7.57, "rank": 0.0, "recommend": 14.41, "export": 0.01, "wall_clock_total": 408.59}
- Summed call latencies are not used as wall-clock time; wall-clock is measured around each whole run.

## 2. Full-run projection (estimate, not measured)

Scope: 660,622 rows accounted for; 660,609 nonempty classifications; 13 empty-text quarantines; exact-text reuse reduces first-pass work to 484,189 distinct texts.

Controls: total budget **$9.00** (Anthropic cap $4.50, TypeSafe cap $4.50); max in-flight Message Batches (workers) **4**; 200 requests per batch; output-token cap **2000** per request; max retry/fallback fraction **5%** (same model; no stronger-model fallback is configured); verify sample 200; Jev rate 8.0/s.

| scenario | texts | retry | requests | batches | enrich $ | verify $ | group+memo $ | **total $** | enrich at standard tier $ | worst case at output cap $ | hours | budget |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 484,189 | 1% | 9,781 | 49 | 4.31 | 0.0065 | 0.0361 | **4.35** | 8.61 | 6.60 | 1.4 | ok |
| base_no_reuse | 660,609 | 1% | 13,345 | 67 | 5.88 | 0.0065 | 0.0361 | **5.92** | 11.75 | 9.01 | 1.8 | **OVER BUDGET** |
| conservative | 484,189 | 5% | 10,168 | 51 | 4.48 | 0.0067 | 0.0361 | **4.52** | 8.95 | 6.86 | 4.2 | **OVER BUDGET** |
| conservative_no_reuse | 660,609 | 5% | 13,874 | 70 | 6.11 | 0.0067 | 0.0361 | **6.15** | 12.22 | 9.36 | 5.8 | **OVER BUDGET** |

Assumptions: per-text enrichment cost, retry fraction and batch turnaround are the measured means of the source run (pilot unless calibrated); base retry = max(measured, 1%) capped by the fallback cap; conservative retry = max(3x measured, 5%, cap) and 3x batch turnaround. Time = ceil(batches / workers) x turnaround + verify + fixed overhead (ingest, group, memo once). Verification and group/memo are never multiplied by the review count.

## Batching and caching: what each word means here

- **CSV chunks** (memory): ingestion streams the CSV into SQLite; this does not affect API cost.
- **Multi-review requests**: each enrichment request carries <= 50 reviews, so the ~3k-token instructions are paid once per 50 reviews; every returned id is validated.
- **Provider Batch API**: those requests are submitted through the Message Batches API at 50% of standard price, with asynchronous turnaround (measured above).
- **Saved-result cache** (ours): exact review text + identical label_config reuses one validated result (`cache_source_id`); every original ID stays a separate record.
- **Provider prompt caching** (Anthropic's): the repeated system prompt is billed at cache-write/cache-read rates when the provider reports it; only reported usage is counted.

## Replay
- Offline (default): `python cost/calculator.py` (edit `cost/rates.csv` or pass flags)
- Paid pilot (explicit): `python cost/calculator.py pilot --confirm-paid`
- Self-test (offline): `python cost/calculator.py selftest` (2x rates -> 2x API cost, same time; projected volume does not change measured results)

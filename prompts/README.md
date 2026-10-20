# Role prompts (active vs. superseded)

| Role | Model | Prompt / instructions | Input (saved) | Output (saved) |
|---|---|---|---|---|
| **enrich** | claude-haiku-5-5 (effort low, thinking disabled, ≤ 2,000 output tokens, Message Batches) | [enrich_codes_v1.md](enrich_codes_v1.md) | ≤ 50 review texts per request (IDs → local r1..r50); request bodies logged by review IDs in `calls` | coded lines → validated records (`runs/full/enriched.jsonl.gz` release asset, `grading/records.jsonl.gz`) |
| **verify** | Jev / TypeSafe (`jev-latest` → jev-1.13.0) | [jev_questions_v2.json](jev_questions_v2.json) (Choice for topic/intent/severity, Score for sentiment) | one review text per request, never the first label | `runs/full/verify/verifier_predictions.jsonl`, `disagreements.csv`, `verify_report.json` |
| **group** (names only) | claude-haiku-5-5 | [group_v1.md](group_v1.md) | `runs/full/group_evidence_pack.json` (bounded examples per issue) | `runs/full/issues.json` (membership itself is code: `membership.csv`) |
| **memo** | claude-sonnet-5-5 (effort medium) | [memo_v2.md](memo_v2.md) | `runs/full/memo_evidence_pack.json` (aggregates + claim IDs + a few quotes) | `memo.json`, `memo.md`, checked by code → `memo_check.json` |

Superseded (kept for the measured design history, not used by the final run):
- `enrich_fallback_v1.md`: the JSON-output version of the enrichment prompt. Measured at 61–87 output tokens per review, versus ≈ 18 for the coded format.
- `jev_questions_v1.json`: the original longer Jev question set (Jev was later moved from enrichment to verification).
- `verify_v1.md`: the Haiku verifier prompt, replaced by Jev for independence (a different model from a different provider).

Prompt versions and SHA-256 digests are recorded per run in `runs/full/data_manifest.json`. The enrichment prompt digest is part of `label_config`.

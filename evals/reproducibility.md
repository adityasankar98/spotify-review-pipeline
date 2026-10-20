# Reproducibility and recovery evidence (actual outcomes)

| Check | How | Result |
|---|---|---|
| Ranking rebuilt from saved files, twice | `python -m pipeline rank --records grading/records.jsonl.gz --membership grading/membership.csv --out <dir>` ×2 | all three `ranking.csv` files have SHA-256 `50fb320bb0e60856f77d458670923c9c9bdd0369d929cc3422b02bf3ed7ca7ac` (byte-identical) |
| Clean environment, no keys | fresh `git clone` from GitHub, fresh Python 3.12 venv from `requirements.txt`, no `.env`, dataset copied into `data/raw/` | offline calculator replay ✓, `calculator.py selftest` ✓ (2x rates → exactly 2x cost; volume change leaves pilot results unchanged), ranking rebuild identical ✓, golden eval reproduced ✓, course checker 0.9999 / 660,503 completed ✓ |
| Warm rerun of the 100-review pilot | same command on saved results | 0 new enrichment calls, 0 downstream calls; 0.13 s vs. 449 s cold (`cost/measurements.json`) |
| Interruption and resume (full run) | SIGINT after the first batches were collected; identical command rerun ([recording](../runs/full/recording/)) | completed 206,059 → 660,503; 5 open batches collected, not resubmitted; **0** of the 8,135 enrichment calls submitted by the resume contain a review ID completed before the interrupt |
| Spend controls | ledger reserves worst-case cost before each batch; per-provider caps $4.50 | admission paused and resumed under a tight cap (simulated test) and never exceeded a cap in real runs; logged spend matches both provider consoles (`cost/reconciliation.json`) |
| Retry / recovery controls | system tests T4 (injected 429 → backoff → success) and T5 (malformed output → one retry → quarantine) on real APIs | pass ([system/report.md](system/report.md)) |
| Memo ↔ saved calculations | `python tools/check_memo.py` | 26 claim-cited numbers equal their saved values; all other numbers in the evidence pack; 4 review IDs exist with verbatim quotes |

Model outputs are not perfectly deterministic (one development review flipped between praise and complaint across two test
runs). Reproducibility therefore comes from saved, versioned results (`label_config`) rather than from re-asking the model.

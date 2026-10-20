# Course self-check (zero-API) on the final grading export

Commands, run from the repository root with the dataset in `data/raw/`, exactly as `GRADING_CONTRACT.md` specifies
(the reference file and the report stay outside `grading/`):

```bash
python3 tools/check_submission.py profile --full data/raw/spotify_reviews_18months.csv --out /tmp/profile.json
python3 tools/check_submission.py reference --full data/raw/spotify_reviews_18months.csv --analysis data/raw/spotify_reviews_18months.csv --out /tmp/local-reference.json
python3 tools/check_submission.py check --reference /tmp/local-reference.json --submission grading --out evals/self_check.json
```

Result ([self_check.json](self_check.json)): status **review_required**, working coverage point candidate **0.9999**.

| check | value |
|---|---|
| expected / received IDs | 660,622 / 660,622 |
| missing / duplicate / invalid | 0 / 0 / 0 |
| valid completed | 660,503 (0.99982 of all rows; 0.99984 of nonempty) |
| quarantined | 119 |
| valid exact-text cache reuses | 176,420 |
| reported calls / input / output tokens | 9,937 / 23,904,295 / 8,972,825 |
| flags | {'unfinished_classification': 1} (unfinished_classification: 106) |

`grading/ingestion.json` is byte-for-byte the helper's `profile` output for the full file. The only flag is the 106 enrichment
quarantines (malformed model output after one retry); they are reported as incomplete classifications, not hidden.
Status `review_required` reflects that flag; per the contract it identifies missing evidence and does not by itself set a grade.

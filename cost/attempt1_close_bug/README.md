# Pilot attempt 1 (2026-10-18 19:57): real run, kept as failure evidence

All stages completed on the real APIs (100/100 enriched via Message Batches, Jev verify on 20, group, rank, memo, export),
and the warm rerun made 0 new calls. The cold invocation then exited with code 1 at shutdown:
`AttributeError: 'AsyncTypeSafeClient' object has no attribute 'close'`. The TypeSafe SDK names it `aclose()`. Mock tests
had not caught it because the mock client defines `close()`. Fixed in pipeline/providers.py (`Providers.close`), and the
pilot was rerun from an empty cache; the rerun is the measured pilot in `cost/`. Spend for this attempt: $0.0376 (logged in
data/state/pilot100.sqlite before it was reset; per-call usage in pilot_calls.jsonl here).

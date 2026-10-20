# Explain it in your own words: a one-page study guide

## The question
Spotify has a quarter of product effort to spend. Should it go to **access, usability, playback, or billing/support**?
The evidence is 660,622 Google Play reviews (May 2022 – Nov 2023). The answer has to be *inspectable*: every number in
the memo traces back to saved records that anyone can recompute without paying for a model call.

## The pipeline in one breath
**Code** reads every row, sets aside the 13 empty reviews, and collapses identical texts (660,609 → 484,189 distinct).
**Haiku** labels each distinct text: topic, intent, severity, sentiment, needs_review. **Code** checks every label,
pulls an exact quote, and copies the result to identical reviews. **Jev**, a different model from a different company,
re-labels a random sample blind, and code counts the disagreements. **Code** groups complaints into issues and ranks
them by count × average severity. **Sonnet** reads only those totals plus a few quotes and writes the memo. **Code**
checks that every number and ID it cites exists.

## Who does what (the most important idea)
| Code (deterministic, free, checkable) | Models (judgment on messy language) |
|---|---|
| Parsing, IDs, hashes, dedup, the work queue | Haiku: what is this review about, and how bad is it? |
| Validating every model line; one retry, then quarantine | Jev: an independent second opinion on a sample |
| Evidence quote and entities (exact substrings) | Haiku: short names for issue groups |
| Spend ledger, rate limits, retries, resume | Sonnet: write the argument from the totals |
| Grouping, counting, ranking, every number in the memo | |

The rule: **models read language; code owns accounting and arithmetic.** A model never adds up anything we report.

## Three decisions to be able to defend (all measured, not guessed)
1. **Why Haiku with batches instead of Jev for bulk labeling?** Jev answers one review per request, so our ~730
   tokens of instructions were resent with every review: about $15 for the full run, which is over budget. Haiku reads
   50 reviews per request, so the instructions are paid once per 50. Asking for short codes (`r4 P crash_freeze C 4 -2 0`)
   instead of JSON cut output from 61 to about 15 tokens per review. The Batch API halves the price again. Result: the full
   run's enrichment measured **$3.57** (the 10k checkpoint had projected $3.54).
2. **Why is Jev the verifier?** Independence. A second model from a different provider, with different instructions,
   never shown the first answer, catches errors that the same model would just repeat. A sample is enough to measure
   agreement, so it costs pennies.
3. **Why exact-text caching?** 176k reviews duplicate another review's text ("Good", "Nice app"...). Labeling each
   distinct text once and copying the result (with `cache_source_id` provenance) cut the work by 27% (484k of 660k texts) without merging
   any counts. Every review still counts once in every total.

## How stop-and-resume works
Each request's results commit to SQLite in one transaction, together with its call-log row. Submitted batches are
recorded before their results arrive. On Ctrl-C the program stops submitting and exits. On restart it **collects
the open batches instead of resubmitting them** (no double charge), skips everything completed, and continues.
`checkpoint_before.json` / `checkpoint_after.json` are the proof.

## One failure to talk about (pick one)
- **Malformed model output** (system test T5): two responses were deliberately garbled. The reviews whose lines
  were missing got one retry round. 3 then completed, and 4 failed twice and were **quarantined with the reason**,
  never silently dropped and never guessed. In the full run, 106 reviews (0.016%) ended up quarantined this way.
- **Nondeterminism**: the same review came back *praise* in one test run and *complaint* in another. That's why results are
  cached under a fixed `label_config` and the ranking is rebuilt from saved outputs, not by re-asking the model.
- **Jev looked cheapest per token but was the most expensive per review**: the per-request overhead dominated.

## What the evaluation does and doesn't show
- **Golden 50** (your labels): agreement per field, severity error, a confusion table and every disagreement.
  It's a small diagnostic, not a precise accuracy estimate. The setup was frozen before scoring.
- **Verifier**: how often two independent models agree on a random sample. Disagreement shows where the labels are uncertain.
- **Limits**: reviews are self-selected and historical. Stars aren't severity. "I'm cancelling" is *stated intent*, not
  observed churn. There's no revenue data, so no revenue-at-risk claims.

## Likely questions
- *Is this multi-agent?* Yes: four distinct roles (enrich, verify, group, memo), each with its own instructions,
  inputs, outputs and saved evidence, coordinated by code. Code decides what runs next; models only judge language.
- *What stops it overspending?* The ledger reserves the worst-case cost before every batch, and work stops when spent +
  reserved + the next reservation would pass the per-provider caps ($4.50 each, under your $5 prepaid balances).
- *Could the ranking change if you reran it?* Not from saved outputs: `python -m pipeline rank` gives byte-identical
  results. A fresh model rerun could differ slightly, which is why we don't rerun.

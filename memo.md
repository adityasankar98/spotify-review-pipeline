# Decision memo: where Spotify's next quarter of product effort should go

Final memo, revised by the student from the AI-generated draft (memo_ai_original.md). Every issue-level and area-level
number cites a claim ID from runs/full/claims.csv or topic_claims.csv, computed by code from the full run, and
`python tools/check_memo.py` verifies each one against the saved calculations.

## Recommendation

**Fund playback reliability.** The two issues to fix first are music that stops, pauses or skips by itself
(ISS-playback-stops_pauses_skips: 14,242 complaints [C013], mean severity 3.327763 [C015]) and crashes and freezes
(ISS-playback-crash_freeze: 9,863 complaints [C017], mean severity 3.605799 [C019]). **Free-tier restrictions should not
be "fixed" with engineering effort.** They should go to a measured pricing experiment, because this data cannot see their revenue effect.

## Why, and how this squares with the baseline ranking

The required baseline (count × mean severity) ranks generic criticism first (ISS-other-general_negative, 86,666 complaints
[C001]). That issue names no specific problem, so it can't be funded. Next comes ISS-billing-free_tier_restrictions (score 138,070 [C008]),
ahead of playback stops (score 47,394 [C016]). We depart from that order deliberately, for three reasons:

1. **Severity, not volume.** The baseline is a severity *sum*, so two mild annoyances count the same as one blocked user. Measured by
   complaints where a core task is clearly blocked (severity 4+), playback has 15,390 [T022], more than three times
   billing/support's 4,853 [T010], with a higher mean severity (3.289719 [T021] vs. 2.876663 [T009]). A quarter of product
   effort should go where the product is broken.
2. **No revenue trade-off, and paying users are affected.** One subscriber wrote: "I upgraded eventually to the paid version to get
   rid of ads ... It stops playing music after 1 song, even if there's more on the queue." [R:58af4b3e-a46c-49ef-a177-74786ae9767e]
   Another: "When l play a song after some time it stop automatically and i am not able to play that song again."
   [R:d3dcc154-712b-4b06-8612-c1bdb90f2eed] Fixing this gives nothing away.
3. **What the data can and can't evaluate.** Free-tier restrictions (47,460 complaints [C005], mean severity 2.909187 [C007]) make
   up most of billing/support's 58,239 complaints [T007]. They're a deliberate paywall ("I can't even select the songs I want."
   [R:1fdec127-9ebc-4cc1-87ab-f23c8e8c0624]). Reviews show the cost of that paywall, not the upgrades it produces, so they can't
   show that removing it helps the business.

## Alternatives considered

- **Billing/support (free tier):** the strongest alternative. It has the most cancellation language, 12,786 records [T011], and its
  complaint share rose from 3.51% (6,165 of 175,690 completed reviews, Jun–Nov 2022) to 13.12% (39,530 of 301,358, May–Oct 2023),
  full months only. Recommended action: an experiment on free-tier packaging (for example, restoring song choice for a test cohort),
  judged on conversion and retention data we don't have. Cancellation language is stated intent, not observed churn.
- **Access:** the highest mean severity, 3.868684 [T033], and 8,989 blocked-task complaints [T034], mostly login failure (8,528
  complaints [C021]). Reach is small (3.5% of complaints [T036]), but it's a cheap, targeted fix we'd pair with the playback work:
  "I'm not able to even log-in" [R:1e451504-cbd5-4857-b82a-2123dcdac002].
- **Usability:** the largest specific area by volume (66,061 complaints [T013]) but the lowest severity (2.390730 [T015]; 1,491
  blocked-task complaints [T016]). Its biggest issue, ads frequency (36,113 complaints [C009]), is a monetization lever like the free tier.

**Counterpoint we accept:** playback's share of reviews fell from 7.56% to 4.09% between those periods, while generic and billing
complaints surged. It is still the largest pool of users who cannot use the product.

## Limitations

- Reviews are self-selected, public and historical. They aren't a representative sample of Spotify users or revenue.
- There's no revenue, plan-tier or churn data, so we make no revenue-at-risk claims and give no conversion effect for the free tier.
- Of 660,622 reviews, 660,503 were classified and 119 are unresolved: 13 empty texts, and the rest malformed model output after one retry.
  176,420 were completed via exact-text reuse, and 104,769 are flagged needs-review.
- Label noise is material at topic level (verifier topic agreement 0.75, golden-set 0.62). The billing/usability boundary for
  premium-only controls is a judgment call that moves complaints between those two areas.
- Severity 4+ counts depend on the model's severity labels (verifier severity MAE 0.26).

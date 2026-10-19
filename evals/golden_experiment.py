"""Paid golden-set setup comparison (explicit: --confirm-paid). 50 Jev calls + 1 Haiku call.

Only review texts are sent; the human labels file is never opened by this script.
Produces predictions for three enrichment setups on the same 50 reviews:
  jev     - Jev labels + deterministic quote/entities (no fallback)
  haiku   - Haiku fallback prompt applied to all 50
  hybrid  - the production routing rule: Haiku where Jev routes the text, else Jev
    python evals/golden_experiment.py --confirm-paid
"""
import asyncio
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

from pipeline import enrich, settings  # noqa: E402
from pipeline.providers import Ledger, Providers  # noqa: E402
from pipeline.store import Store  # noqa: E402

OUT = ROOT / "evals" / "golden"


async def main():
    load_dotenv(ROOT / ".env")
    rows = list(csv.DictReader(open(OUT / "golden_50_input.csv", encoding="utf-8", newline="")))
    store = Store(settings.STATE_DIR / "golden_experiment.sqlite")
    inv, _ = store.start_invocation(sys.argv)
    opts = dict(settings.DEFAULTS, jev_rps=5.0)
    prov = Providers(store, Ledger(store, 1.0), settings.load_rates(), opts, inv, "initial", print)
    try:
        jev = await asyncio.gather(*[prov.jev_classify(r["review_id"], r["review_text"], "golden") for r in rows])
        fb = await enrich._fallback(prov, opts, [(r["review_id"], r["review_text"]) for r in rows], "golden")
    finally:
        await prov.close()
    preds = {"jev": [], "haiku": [], "hybrid": []}
    for r, j in zip(rows, jev):
        lab, det = enrich.from_jev(r["review_text"], j)
        hl = enrich.apply_fallback(r["review_text"], dict(lab), json.loads(json.dumps(det)), fb[r["review_id"]])
        routed = bool(det["route_reasons"])
        base = {"review_id": r["review_id"]}
        preds["jev"].append({**base, **lab, "route_reasons": det["route_reasons"], "jev_conf": det["conf"]})
        preds["haiku"].append({**base, **hl})
        preds["hybrid"].append({**base, **(hl if routed else lab), "routed_to_fallback": routed})
    for k, v in preds.items():
        (OUT / f"predictions_{k}.jsonl").write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in v) + "\n")
    calls = [dict(c) for c in store.db.execute("SELECT * FROM calls")]
    (OUT / "experiment_calls.jsonl").write_text("\n".join(json.dumps(c, default=str) for c in calls) + "\n")
    print(f"saved predictions for {len(rows)} reviews; spent ${store.spent_usd():.4f}; routed {sum(p['routed_to_fallback'] for p in preds['hybrid'])}")
    store.end_invocation(inv, "completed")


if __name__ == "__main__":
    if "--confirm-paid" not in sys.argv:
        sys.exit("paid experiment: pass --confirm-paid")
    asyncio.run(main())

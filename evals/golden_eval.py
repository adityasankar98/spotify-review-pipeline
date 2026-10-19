"""Golden-set evaluation (offline, no model calls).

    python evals/golden_eval.py                       # evaluates every evals/golden/predictions_*.jsonl
Compares predictions against evals/golden/golden_50_human.csv (human labels; never a model input).
Ambiguous cases may list accepted alternates (alt_topics / alt_intents / alt_severities, '|' separated).
Missing predictions count as incorrect.
"""
import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
G = ROOT / "evals" / "golden"
TOPICS = ["access", "usability", "playback", "downloads", "catalog", "billing", "support", "other"]
INTENTS = ["cancellation", "complaint", "request", "praise", "unclear"]
SENT_TOL = 0.5


def load_human():
    rows = list(csv.DictReader(open(G / "golden_50_human.csv", encoding="utf-8", newline="")))
    out = {}
    for r in rows:
        if not (r["topic"] and r["intent"] and r["severity"] and r["sentiment"]):
            continue
        alt = lambda k: [x for x in (r.get(k) or "").split("|") if x]
        out[r["review_id"]] = {
            "text": r["review_text"], "topic": r["topic"], "intent": r["intent"], "severity": int(r["severity"]),
            "sentiment": float(r["sentiment"]), "evidence_quote": r["evidence_quote"],
            "entities": [e.strip() for e in r["entities"].split(",") if e.strip()],
            "needs_review": r["needs_review"] == "true", "ambiguous": r["ambiguous"] == "true",
            "accepted_topic": [r["topic"]] + alt("alt_topics"), "accepted_intent": [r["intent"]] + alt("alt_intents"),
            "accepted_severity": [int(r["severity"])] + [int(x) for x in alt("alt_severities")], "notes": r.get("notes", "")}
    return out, len(rows)


def f1_table(pairs, labels):
    res = {}
    for l in labels:
        tp = sum(a == l and p == l for a, p in pairs)
        sup = sum(a == l for a, _ in pairs)
        pred = sum(p == l for _, p in pairs)
        res[l] = {"support": sup, "predicted": pred, "f1": round(2 * tp / (sup + pred), 3) if sup + pred else None}
    vals = [v["f1"] for v in res.values() if v["support"]]
    return res, round(sum(vals) / len(vals), 3) if vals else None


def evaluate(name, preds, human):
    cases, n = [], len(human)
    agg = Counter()
    tpairs, ipairs = [], []
    conf_topic = Counter()
    for rid, h in human.items():
        p = preds.get(rid)
        c = {"review_id": rid, "text": h["text"][:160], "expected": {k: h[k] for k in ("topic", "intent", "severity", "sentiment")},
             "ambiguous": h["ambiguous"]}
        if not p:
            c.update(prediction=None, topic_ok=False, intent_ok=False, severity_exact=False)
            agg["missing"] += 1
            cases.append(c)
            continue
        c["prediction"] = {k: p.get(k) for k in ("topic", "intent", "severity", "sentiment", "evidence_quote", "entities", "needs_review")}
        c["topic_ok"] = p["topic"] in h["accepted_topic"]
        c["intent_ok"] = p["intent"] in h["accepted_intent"]
        c["severity_exact"] = p["severity"] in h["accepted_severity"]
        c["severity_abs_err"] = min(abs(p["severity"] - s) for s in h["accepted_severity"])
        c["sentiment_abs_err"] = round(abs(p["sentiment"] - h["sentiment"]), 3)
        c["quote_is_substring"] = bool(p["evidence_quote"]) and p["evidence_quote"] in h["text"]
        low = h["text"].lower()
        c["unsupported_entities"] = [e for e in p.get("entities", []) if e.lower() not in low]
        hum_ents = {e.lower() for e in h["entities"]}
        c["entity_overlap"] = sorted({e.lower() for e in p.get("entities", [])} & hum_ents)
        c["needs_review_pred"] = bool(p.get("needs_review"))
        c["needs_review_human"] = h["needs_review"] or h["ambiguous"]
        for k in ("topic_ok", "intent_ok", "severity_exact", "quote_is_substring"):
            agg[k] += c[k]
        agg["joint"] += c["topic_ok"] and c["intent_ok"] and c["severity_exact"]
        agg["sev_err"] += c["severity_abs_err"]
        agg["sent_err"] += c["sentiment_abs_err"]
        agg["sent_within"] += c["sentiment_abs_err"] <= SENT_TOL
        agg["unsupported_entities"] += len(c["unsupported_entities"])
        agg["nr_tp"] += c["needs_review_pred"] and c["needs_review_human"]
        agg["nr_fp"] += c["needs_review_pred"] and not c["needs_review_human"]
        agg["nr_fn"] += (not c["needs_review_pred"]) and c["needs_review_human"]
        agg["observed"] += 1
        if not h["ambiguous"]:
            tpairs.append((h["topic"], p["topic"]))
            ipairs.append((h["intent"], p["intent"]))
        conf_topic[(h["topic"], p["topic"])] += 1
        cases.append(c)
    o = max(1, agg["observed"])
    tf1, tmacro = f1_table(tpairs, TOPICS)
    if1, imacro = f1_table(ipairs, INTENTS)
    return {
        "setup": name, "cases": n, "missing_predictions": agg["missing"],
        "ambiguous_cases": sum(h["ambiguous"] for h in human.values()),
        "topic_agreement": round(agg["topic_ok"] / n, 3), "intent_agreement": round(agg["intent_ok"] / n, 3),
        "joint_topic_intent_severity": round(agg["joint"] / n, 3),
        "severity_exact": round(agg["severity_exact"] / n, 3), "severity_mae": round(agg["sev_err"] / o, 3),
        "sentiment_mae": round(agg["sent_err"] / o, 3), f"sentiment_within_{SENT_TOL}": round(agg["sent_within"] / o, 3),
        "quote_exact_substring_rate": round(agg["quote_is_substring"] / o, 3),
        "unsupported_entity_additions": agg["unsupported_entities"],
        "needs_review": {"tp": agg["nr_tp"], "fp": agg["nr_fp"], "fn": agg["nr_fn"]},
        "topic_macro_f1_unambiguous": tmacro, "intent_macro_f1_unambiguous": imacro,
        "topic_per_class": tf1, "intent_per_class": if1,
        "topic_confusion": {f"{a}->{b}": v for (a, b), v in sorted(conf_topic.items()) if a != b},
        "case_results": cases}


def load_preds(path):
    return {json.loads(l)["review_id"]: json.loads(l) for l in open(path, encoding="utf-8") if l.strip()}


def main():
    human, total = load_human()
    print(f"human-labeled cases: {len(human)} / {total}")
    if len(human) < total:
        print("WARNING: golden labels incomplete - results are provisional")
    summary = {}
    for p in sorted(G.glob("predictions_*.jsonl")):
        name = p.stem.replace("predictions_", "")
        r = evaluate(name, load_preds(p), human)
        (G / f"eval_{name}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False))
        summary[name] = {k: v for k, v in r.items() if k not in ("case_results", "topic_per_class", "intent_per_class")}
        print(json.dumps(summary[name]))
    (G / "golden_eval_all.json").write_text(json.dumps(summary, indent=1))
    if "final" in summary:
        (G / "golden_eval_summary.json").write_text(json.dumps(summary["final"], indent=1))


if __name__ == "__main__":
    main()

"""Stage 3 - Verify (role: verify).

Code draws a declared, seeded random sample of completed origin records (lowest SHA-256(seed:id)).
Claude Haiku 5.5, with its own prompt, re-labels each sampled review from the original text only -
it never sees the first prediction. Code then compares field by field and writes a disagreement
report. Results are cached by (sample IDs, prompt version, model) so a warm rerun makes no calls.
"""
import csv
import hashlib
import json

from . import settings
from .providers import BudgetStop, CallFailed

SYSTEM = (settings.PROMPTS / "verify_v1.md").read_text()
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["results"],
          "properties": {"results": {"type": "array", "items": {
              "type": "object", "additionalProperties": False,
              "required": ["id", "topic", "intent", "severity", "sentiment"],
              "properties": {"id": {"type": "string"}, "topic": {"type": "string", "enum": list(settings.TOPICS)},
                             "intent": {"type": "string", "enum": list(settings.INTENTS)},
                             "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                             "sentiment": {"type": "number"}}}}}}
SENTIMENT_TOL = 0.5


def sample_ids(store, n, seed):
    rows = store.db.execute("SELECT review_id FROM records WHERE status='completed' AND cache_source_id IS NULL").fetchall()
    ids = sorted((hashlib.sha256(f"{seed}:{r[0]}".encode()).hexdigest(), r[0]) for r in rows)
    return [rid for _, rid in ids[:n]]


async def verify_ids(prov, opts, items, tag):
    """items: list of (review_id, text). Returns {review_id: verifier labels}."""
    out = {}
    for k in range(0, len(items), 50):
        chunk = items[k:k + 50]
        local = {f"v{i + 1}": rid for i, (rid, _) in enumerate(chunk)}
        user = "Label each review independently.\n<reviews>\n" + json.dumps(
            [{"id": f"v{i + 1}", "text": t} for i, (_, t) in enumerate(chunk)], ensure_ascii=False) + "\n</reviews>"

        def validate(d, local=local):
            ids = [r["id"] for r in d.get("results", [])]
            return None if sorted(ids) == sorted(local) else "verifier returned wrong id set"

        data, _ = await prov.claude_json(role="verify", model=settings.HAIKU, system=SYSTEM, user=user, schema=SCHEMA,
                                         max_tokens=4000, effort="low", review_ids=[rid for rid, _ in chunk],
                                         prompt_version=settings.VERIFY_PROMPT_VERSION, batch_key=f"{tag}-{k // 50}",
                                         validate=validate)
        for r in data["results"]:
            out[local[r["id"]]] = {kk: r[kk] for kk in ("topic", "intent", "severity", "sentiment")}
    return out


def compare(first, ver):
    return {"topic": first["topic"] == ver["topic"], "intent": first["intent"] == ver["intent"],
            "severity_exact": first["severity"] == ver["severity"],
            "severity_abs_diff": abs(first["severity"] - ver["severity"]),
            "sentiment_within_tol": abs(first["sentiment"] - ver["sentiment"]) <= SENTIMENT_TOL}


async def run(store, prov, opts, run_dir, log):
    n, seed = opts["verify_sample"], opts["verify_seed"]
    ids = sample_ids(store, n, seed)
    if not ids:
        log("verify: no completed records to verify")
        return
    key = "verify:" + hashlib.sha256(json.dumps([ids, settings.VERIFY_PROMPT_VERSION, settings.HAIKU]).encode()).hexdigest()
    cached = store.get_artifact(key)
    rows = {r["review_id"]: dict(r) for r in store.db.execute(
        f"SELECT c.*, r.review_text FROM records c JOIN reviews r USING(review_id) WHERE c.review_id IN ({','.join('?' * len(ids))})", ids)}
    if cached:
        ver = cached
        log(f"verify: reusing cached verifier output for {len(ids)} sampled records (0 new calls)")
    else:
        try:
            ver = await verify_ids(prov, opts, [(rid, rows[rid]["review_text"]) for rid in ids], "verify")
        except (CallFailed, BudgetStop) as e:
            log(f"verify: FAILED ({e}); verification incomplete")
            return
        store.put_artifact(key, "verify", ver)
    out = run_dir / "verify"
    out.mkdir(parents=True, exist_ok=True)
    per, dis = [], []
    for rid in ids:
        f = rows[rid]
        c = compare(f, ver[rid])
        per.append({"review_id": rid, "first": {k: f[k] for k in ("topic", "intent", "severity", "sentiment")},
                    "label_source": f["label_source"], "verifier": ver[rid], **c})
        if not (c["topic"] and c["intent"]) or c["severity_abs_diff"] >= 2:
            dis.append([rid, f["review_text"][:300], f["topic"], ver[rid]["topic"], f["intent"], ver[rid]["intent"],
                        f["severity"], ver[rid]["severity"], f["label_source"]])
    m = len(per)
    report = {"sample_size": m, "sample_rule": f"lowest sha256('{seed}:'+review_id) among completed origin records",
              "verifier_model": settings.HAIKU, "verifier_prompt": settings.VERIFY_PROMPT_VERSION,
              "agreement": {"topic": round(sum(p["topic"] for p in per) / m, 4), "intent": round(sum(p["intent"] for p in per) / m, 4),
                            "severity_exact": round(sum(p["severity_exact"] for p in per) / m, 4),
                            "severity_mae": round(sum(p["severity_abs_diff"] for p in per) / m, 4),
                            f"sentiment_within_{SENTIMENT_TOL}": round(sum(p["sentiment_within_tol"] for p in per) / m, 4)},
              "disagreements_flagged": len(dis),
              "flag_rule": "topic or intent differs, or severity differs by >= 2"}
    (out / "verifier_predictions.jsonl").write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in per) + "\n")
    with (out / "disagreements.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["review_id", "text_excerpt", "first_topic", "verifier_topic", "first_intent", "verifier_intent",
                    "first_severity", "verifier_severity", "first_label_source"])
        w.writerows(dis)
    (out / "verify_report.json").write_text(json.dumps(report, indent=2))
    log(f"verify: {m} sampled; agreement {report['agreement']}; {len(dis)} disagreements flagged")

"""Stage 3 - Verify (role: verify).

Code draws a declared, seeded random sample of completed origin records (lowest SHA-256(seed:id)).
Jev (TypeSafe) - a different model from a different provider than the Haiku enricher - answers its own
fixed questions (prompts/jev_questions_v2.json) about each sampled review's original text only; it never
sees the first prediction. Code maps Jev's choices into the common schema, applies the same deterministic
severity rules from the shared definitions, compares field by field and writes a disagreement report.
Results are cached by (sample IDs, question version, model) so a warm rerun makes no calls.
"""
import asyncio
import csv
import hashlib
import json

from . import settings
from .providers import BudgetStop, CallFailed

SENTIMENT_TOL = 0.5


def sample_ids(store, n, seed):
    rows = store.db.execute("SELECT review_id FROM records WHERE status='completed' AND cache_source_id IS NULL").fetchall()
    ids = sorted((hashlib.sha256(f"{seed}:{r[0]}".encode()).hexdigest(), r[0]) for r in rows)
    return [rid for _, rid in ids[:n]]


def from_jev(j):
    """Jev answers -> common schema. Severity rules from the shared definitions are applied by code."""
    lab = {"topic": j["topic"]["choice"], "intent": j["intent"]["choice"], "severity": int(j["severity"]["choice"]),
           "sentiment": round(j["sentiment"]["score"] / 2 - 1, 2)}
    if lab["intent"] in ("praise", "request", "unclear"):
        lab["severity"] = 1
    elif lab["intent"] == "complaint" and lab["severity"] == 1:
        lab["severity"] = 2
    lab["confidence"] = {k: round(j[k]["confidence"], 3) for k in ("topic", "intent", "severity")}
    return lab


async def verify_ids(prov, opts, items, tag):
    """items: list of (review_id, text). One Jev request per review (rate-limited, bounded retries).
    Returns {review_id: verifier labels}; reviews whose call failed are omitted and reported."""
    async def one(k, rid, text):
        return rid, from_jev(await prov.jev_classify(rid, text, f"{tag}-{k // 50}", role="verify", phase="initial"))
    res = await asyncio.gather(*[one(k, rid, t) for k, (rid, t) in enumerate(items)], return_exceptions=True)
    for r in res:
        if isinstance(r, BudgetStop):
            raise r
    return {r[0]: r[1] for r in res if not isinstance(r, Exception)}


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
    key = "verify:" + hashlib.sha256(json.dumps([ids, settings.JEV_QUESTIONS_VERSION, settings.JEV_MODEL]).encode()).hexdigest()
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
    failed = [rid for rid in ids if rid not in ver]
    for rid in [i for i in ids if i in ver]:
        f = rows[rid]
        c = compare(f, ver[rid])
        per.append({"review_id": rid, "first": {k: f[k] for k in ("topic", "intent", "severity", "sentiment")},
                    "label_source": f["label_source"], "verifier": ver[rid], **c})
        if not (c["topic"] and c["intent"]) or c["severity_abs_diff"] >= 2:
            dis.append([rid, f["review_text"][:300], f["topic"], ver[rid]["topic"], f["intent"], ver[rid]["intent"],
                        f["severity"], ver[rid]["severity"], f["label_source"]])
    m = len(per)
    if not m:
        log(f"verify: verifier returned no labels ({len(failed)} failed)")
        return
    report = {"sample_size": m, "sample_rule": f"lowest sha256('{seed}:'+review_id) among completed origin records",
              "verifier_model": settings.JEV_MODEL, "verifier_prompt": settings.JEV_QUESTIONS_VERSION,
              "first_model": settings.HAIKU, "verifier_failed_ids": failed,
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

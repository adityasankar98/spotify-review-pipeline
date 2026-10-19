"""Stage 6 - Recommend (role: memo).

Input is a bounded evidence pack built by code from saved aggregates (ranking, topic aggregates,
claims, trends, coverage, a few quotes) - never the raw dataset. Claude Sonnet 5.5 writes the memo
and dashboard recommendation cards. Code then checks every cited claim ID, issue ID and review ID
exists in the pack and that every number in the prose appears in the pack; a failing memo is retried
once and otherwise saved as failed (visible), never silently accepted.
"""
import csv
import hashlib
import json
import re
from pathlib import Path

from . import settings
from .providers import BudgetStop, CallFailed

SYSTEM = (settings.PROMPTS / "memo_v2.md").read_text()
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["priority_area", "recommendation", "memo_markdown", "cards"],
          "properties": {
              "priority_area": {"type": "string", "enum": ["access", "usability", "playback", "billing_support", "downloads", "catalog"]},
              "recommendation": {"type": "string"}, "memo_markdown": {"type": "string"},
              "cards": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                    "required": ["title", "rationale", "claim_ids", "review_ids"],
                                                    "properties": {"title": {"type": "string"}, "rationale": {"type": "string"},
                                                                   "claim_ids": {"type": "array", "items": {"type": "string"}},
                                                                   "review_ids": {"type": "array", "items": {"type": "string"}}}}}}}
NUM_RE = re.compile(r"(?<![\w.:-])\d[\d,]*(?:\.\d+)?(?![\w-])")


def _read_csv(p):
    with open(p) as f:
        return list(csv.DictReader(f))


def build_pack(store, run_dir):
    run_dir = Path(run_dir)
    ranking = _read_csv(run_dir / "ranking.csv")
    claims = _read_csv(run_dir / "claims.csv")
    tclaims = _read_csv(run_dir / "topic_claims.csv")
    issues = {i["issue_id"]: i for i in json.loads((run_dir / "issues.json").read_text())["issues"]}
    cid = {(c["issue_id"], c["metric"]): c["claim_id"] for c in claims}
    top = []
    for r in ranking[:12]:
        i = issues.get(r["issue_id"], {})
        top.append({"rank": r["rank"], "issue_id": r["issue_id"], "name": i.get("name", ""), "description": i.get("description", ""),
                    **{m: {"value": r[m], "claim_id": cid.get((r["issue_id"], m))} for m in
                       ("complaint_count", "severity_sum", "mean_severity", "priority_score")}})
    quotes = []
    for r in ranking[:6]:
        for rid in issues.get(r["issue_id"], {}).get("example_review_ids", [])[:3]:
            row = store.db.execute("SELECT evidence_quote, severity FROM records WHERE review_id=?", (rid,)).fetchone()
            quotes.append({"issue_id": r["issue_id"], "review_id": rid, "quote": row[0][:200], "severity": row[1]})
    summ = json.loads((run_dir / "run_summary.json").read_text()) if (run_dir / "run_summary.json").exists() else {}
    verify = json.loads((run_dir / "verify" / "verify_report.json").read_text()) if (run_dir / "verify" / "verify_report.json").exists() else {}
    trends = json.loads((run_dir / "trends_comparison.json").read_text()) if (run_dir / "trends_comparison.json").exists() else {}
    db = store.db
    coverage = {"source_rows": store.count(), "completed": store.count("completed"), "quarantined": store.count("quarantined"),
                "pending_unresolved": store.count("pending"),
                "empty_text_quarantines": db.execute("SELECT COUNT(*) FROM records WHERE reason='empty_review_text'").fetchone()[0],
                "completed_via_exact_text_reuse": db.execute("SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL").fetchone()[0],
                "needs_review_flagged": db.execute("SELECT COUNT(*) FROM records WHERE needs_review=1").fetchone()[0],
                "complaint_or_cancellation_records": sum(int(r["complaint_count"]) for r in ranking)}
    golden = {}
    gp = settings.ROOT / "evals" / "golden" / "golden_eval_summary.json"
    if gp.exists():
        g = json.loads(gp.read_text())
        golden = {"cases": g.get("cases"), "topic_agreement": g.get("topic_agreement"), "intent_agreement": g.get("intent_agreement"),
                  "severity_mae": g.get("severity_mae")}
    return {"question": "Where should next quarter's product effort go: access, usability, playback, or billing/support?",
            "baseline_rule": "priority_score = complaint_count x mean_severity = severity_sum over complaint+cancellation records; one issue per review",
            "issue_ranking_top12": top, "topic_group_claims": tclaims, "trend_comparison_full_months": trends,
            "coverage": coverage, "verifier_agreement": verify.get("agreement", {}), "verifier_sample_size": verify.get("sample_size"),
            "golden_set_agreement": golden, "example_quotes": quotes}


def check_memo(data, pack):
    blob = json.dumps(pack, ensure_ascii=False)
    allowed_nums = {n.replace(",", "") for n in NUM_RE.findall(blob)} | {str(i) for i in range(0, 11)} | {"2022", "2023"}
    claim_ids = {c for c in re.findall(r'"claim_id": "([CT]\d{3})"', blob)}
    review_ids = {q["review_id"] for q in pack["example_quotes"]}
    issue_ids = {r["issue_id"] for r in pack["issue_ranking_top12"]}
    problems = []
    text = data["memo_markdown"] + " " + data["recommendation"] + " " + " ".join(c["rationale"] for c in data["cards"])
    for c in set(re.findall(r"\[([CT]\d{3})\]", text)) | {c for card in data["cards"] for c in card["claim_ids"]}:
        if c not in claim_ids:
            problems.append(f"unknown claim id {c}")
    for r in set(re.findall(r"\[R:([^\]]+)\]", text)) | {r for card in data["cards"] for r in card["review_ids"]}:
        if r not in review_ids:
            problems.append(f"review id not in evidence pack: {r}")
    for iid in set(re.findall(r"ISS-[a-z_]+-[a-z_]+", text)):
        if iid not in issue_ids:
            problems.append(f"unknown issue id {iid}")
    stripped = re.sub(r"\[(?:R:[^\]]+|[CT]\d{3})\]", " ", text)
    stripped = re.sub(r"ISS-[a-z_]+-[a-z_]+", " ", stripped)
    for n in NUM_RE.findall(stripped):
        if n.replace(",", "") not in allowed_nums:
            problems.append(f"number not in evidence pack: {n}")
    if not re.search(r"\[C\d{3}\]", data["memo_markdown"]):
        problems.append("memo cites no issue claim IDs")
    return sorted(set(problems))


async def run(store, prov, opts, run_dir, log):
    run_dir = Path(run_dir)
    pack = build_pack(store, run_dir)
    (run_dir / "memo_evidence_pack.json").write_text(json.dumps(pack, indent=1, ensure_ascii=False))
    key = "memo:" + hashlib.sha256(json.dumps([pack, settings.MEMO_PROMPT_VERSION, settings.SONNET]).encode()).hexdigest()
    data = store.get_artifact(key)
    if data:
        log("memo: reusing cached memo for identical evidence pack (0 new calls)")
    else:
        try:
            data, _ = await prov.claude_json(role="memo", model=settings.SONNET, system=SYSTEM,
                                             user="<evidence_pack>\n" + json.dumps(pack, ensure_ascii=False) + "\n</evidence_pack>",
                                             schema=SCHEMA, max_tokens=12000, effort="medium", review_ids=[],
                                             prompt_version=settings.MEMO_PROMPT_VERSION, batch_key="memo",
                                             thinking_disabled=False,
                                             validate=lambda d: "; ".join(check_memo(d, pack)[:8]) or None)
            store.put_artifact(key, "memo", data)
        except (CallFailed, BudgetStop) as e:
            log(f"memo: FAILED ({e}) - no memo accepted")
            (run_dir / "memo_check.json").write_text(json.dumps({"status": "failed", "error": str(e)}, indent=1))
            return
    problems = check_memo(data, pack)
    (run_dir / "memo.json").write_text(json.dumps(data, indent=1, ensure_ascii=False))
    (run_dir / "memo_check.json").write_text(json.dumps({"status": "pass" if not problems else "fail", "problems": problems,
                                                         "checked": ["claim ids", "issue ids", "review ids", "numbers"]}, indent=1))
    claims = _read_csv(run_dir / "claims.csv") + _read_csv(run_dir / "topic_claims.csv")
    table = "\n".join(f"| {c['claim_id']} | {c.get('issue_id') or c.get('group')} | {c['metric']} | {c['value']} |" for c in claims)
    md = (f"{data['memo_markdown'].strip()}\n\n---\n*Generated by {settings.SONNET} ({settings.MEMO_PROMPT_VERSION}) from "
          f"`memo_evidence_pack.json`; automated check: **{'pass' if not problems else 'FAIL'}** (`memo_check.json`). "
          f"Claim values below are computed by code (`rank.py`).*\n\n| claim | issue / group | metric | value |\n|---|---|---|---|\n{table}\n")
    (run_dir / "memo.md").write_text(md)
    log(f"memo: priority={data['priority_area']}; check={'pass' if not problems else problems}")

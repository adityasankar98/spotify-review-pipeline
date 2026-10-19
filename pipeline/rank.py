"""Stage 5 - Rank. Code only; never calls a model.

Baseline (contract): over completed complaint/cancellation records and the saved membership,
  complaint_count = members, severity_sum = sum(severity), mean = sum/count (6 dp, half-up),
  priority_score = severity_sum (== count x mean exactly). Sort by -score, then issue_id.

Also writes topic-level aggregates for the product-area question, monthly trends with
denominators (full calendar months only), and the claims tables the memo must cite.

Offline reproduction from exported files:
  python -m pipeline rank --records grading/records.jsonl.gz --membership grading/membership.csv --out /tmp/rerank
"""
import csv
import gzip
import json
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

TOPIC_GROUPS = {"access": "access", "usability": "usability", "playback": "playback", "downloads": "downloads",
                "catalog": "catalog", "billing": "billing_support", "support": "billing_support", "other": "other"}
TOP_CLAIM_ISSUES = 10


def mean6(total, n):
    return str((Decimal(total) / Decimal(n)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def compute_ranking(records, membership):
    """records: {review_id: {intent, severity, ...}} (completed only); membership: [(issue_id, review_id)]."""
    seen, members = set(), defaultdict(list)
    for iid, rid in membership:
        r = records.get(rid)
        if r is None or r["intent"] not in ("complaint", "cancellation") or (iid, rid) in seen:
            raise ValueError(f"invalid membership row {iid},{rid}")
        seen.add((iid, rid))
        members[iid].append(r["severity"])
    rows = [{"issue_id": iid, "complaint_count": len(v), "severity_sum": sum(v), "mean_severity": mean6(sum(v), len(v)),
             "priority_score": sum(v)} for iid, v in members.items()]
    rows.sort(key=lambda x: (-x["priority_score"], x["issue_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def topic_aggregates(records):
    agg = defaultdict(Counter)
    total_completed = len(records)
    total_complaints = sum(1 for r in records.values() if r["intent"] in ("complaint", "cancellation"))
    for r in records.values():
        for g in (TOPIC_GROUPS[r["topic"]], "topic:" + r["topic"]):
            a = agg[g]
            a["completed_records"] += 1
            if r["intent"] in ("complaint", "cancellation"):
                a["complaint_count"] += 1
                a["severity_sum"] += r["severity"]
                a["severity_4plus"] += r["severity"] >= 4
                a["cancellation_count"] += r["intent"] == "cancellation"
            a["request_count"] += r["intent"] == "request"
            a["praise_count"] += r["intent"] == "praise"
    out = []
    for g, a in agg.items():
        c = a["complaint_count"]
        out.append({"group": g, "completed_records": a["completed_records"], "complaint_count": c,
                    "severity_sum": a["severity_sum"], "mean_severity": mean6(a["severity_sum"], c) if c else "",
                    "severity_4plus_count": a["severity_4plus"], "cancellation_count": a["cancellation_count"],
                    "request_count": a["request_count"], "praise_count": a["praise_count"],
                    "share_of_all_complaints_pct": f"{100 * c / total_complaints:.1f}" if total_complaints else "",
                    "denominator_complaints": total_complaints, "denominator_completed": total_completed})
    out.sort(key=lambda x: (-x["severity_sum"], x["group"]))
    return out


def monthly_trends(records):
    """Complaint share by topic group per full calendar month (first/last months are partial: excluded)."""
    months = sorted({r["month"] for r in records.values()})
    full = months[1:-1] if len(months) > 2 else months
    den, num = Counter(), Counter()
    for r in records.values():
        if r["month"] in full:
            den[r["month"]] += 1
            if r["intent"] in ("complaint", "cancellation"):
                num[(r["month"], TOPIC_GROUPS[r["topic"]])] += 1
    groups = sorted(set(TOPIC_GROUPS.values()))
    rows = [{"month": m, "completed_records": den[m], **{f"{g}_complaints": num[(m, g)] for g in groups},
             **{f"{g}_share_pct": f"{100 * num[(m, g)] / den[m]:.2f}" if den[m] else "" for g in groups}} for m in full]
    # comparable periods: first 6 vs last 6 full months
    def period(ms):
        d = sum(den[m] for m in ms)
        return {"months": ms, "completed_records": d,
                **{g: {"complaints": sum(num[(m, g)] for m in ms),
                       "share_pct": f"{100 * sum(num[(m, g)] for m in ms) / d:.2f}" if d else ""} for g in groups}}
    comparison = {"excluded_partial_months": [m for m in months if m not in full],
                  "early": period(full[:6]), "late": period(full[-6:])} if len(full) >= 12 else {"note": "fewer than 12 full months"}
    return rows, comparison


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_outputs(out, records, membership, issues_meta, log, trends=True):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    ranking = compute_ranking(records, membership)
    write_csv(out / "ranking.csv", ranking, ["rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score"])
    # issue aggregates with extra (non-baseline) columns for analysis
    extra = defaultdict(Counter)
    for iid, rid in membership:
        r = records[rid]
        extra[iid]["cancellation_count"] += r["intent"] == "cancellation"
        extra[iid]["severity_4plus_count"] += r["severity"] >= 4
        extra[iid]["needs_review_count"] += bool(r.get("needs_review"))
    total = sum(x["complaint_count"] for x in ranking)
    agg = [{**x, "topic": x["issue_id"].split("-")[1], "name": issues_meta.get(x["issue_id"], {}).get("name", ""),
            **extra[x["issue_id"]], "share_of_complaints_pct": f"{100 * x['complaint_count'] / total:.1f}"} for x in ranking]
    write_csv(out / "aggregates.csv", agg, ["rank", "issue_id", "topic", "name", "complaint_count", "severity_sum", "mean_severity",
                                            "priority_score", "share_of_complaints_pct", "cancellation_count",
                                            "severity_4plus_count", "needs_review_count"])
    topics = topic_aggregates(records)
    write_csv(out / "topic_aggregates.csv", topics, list(topics[0].keys()) if topics else ["group"])
    claims = []
    for x in ranking[:TOP_CLAIM_ISSUES]:
        for metric in ("complaint_count", "severity_sum", "mean_severity", "priority_score"):
            claims.append({"claim_id": f"C{len(claims) + 1:03d}", "issue_id": x["issue_id"], "metric": metric, "value": str(x[metric])})
    write_csv(out / "claims.csv", claims, ["claim_id", "issue_id", "metric", "value"])
    tclaims = []
    for t in topics:
        if t["group"].startswith("topic:"):
            continue
        for metric in ("complaint_count", "severity_sum", "mean_severity", "severity_4plus_count", "cancellation_count",
                       "share_of_all_complaints_pct"):
            tclaims.append({"claim_id": f"T{len(tclaims) + 1:03d}", "group": t["group"], "metric": metric, "value": str(t[metric])})
    write_csv(out / "topic_claims.csv", tclaims, ["claim_id", "group", "metric", "value"])
    if trends and all("month" in r for r in records.values()):
        rows, comparison = monthly_trends(records)
        if rows:
            write_csv(out / "trends_monthly.csv", rows, list(rows[0].keys()))
        (out / "trends_comparison.json").write_text(json.dumps(comparison, indent=1))
    log(f"rank: {len(ranking)} issues ranked from {len(membership)} memberships; top = "
        + ", ".join(f"{x['issue_id']}({x['priority_score']})" for x in ranking[:3]))
    return ranking


def run_from_store(store, prov, opts, run_dir, log):
    recs = {r["review_id"]: {"intent": r["intent"], "severity": r["severity"], "topic": r["topic"],
                             "needs_review": r["needs_review"], "month": r["review_timestamp"][:7]}
            for r in store.db.execute("""SELECT c.review_id, intent, severity, topic, needs_review, review_timestamp
                                         FROM records c JOIN reviews r USING(review_id) WHERE c.status='completed'""")}
    with (Path(run_dir) / "membership.csv").open() as f:
        membership = [(r["issue_id"], r["review_id"]) for r in csv.DictReader(f)]
    meta = {}
    p = Path(run_dir) / "issues.json"
    if p.exists():
        meta = {i["issue_id"]: i for i in json.loads(p.read_text())["issues"]}
    return write_outputs(run_dir, recs, membership, meta, log)


def run_from_files(records_path, membership_path, out, log):
    opener = gzip.open if str(records_path).endswith(".gz") else open
    recs = {}
    with opener(records_path, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("status") == "completed":
                recs[r["review_id"]] = r
    with open(membership_path) as f:
        membership = [(r["issue_id"], r["review_id"]) for r in csv.DictReader(f)]
    return write_outputs(out, recs, membership, {}, log, trends=False)

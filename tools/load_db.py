"""Load a finished run's saved outputs into Postgres (Neon) for the dashboard backend. No model calls.

    python tools/load_db.py --run full            # reads DATABASE_URL_ADMIN (owner) and DATABASE_URL (API role) from .env

The owner role creates the tables; the API role (DATABASE_URL, used by the deployed backend) is granted SELECT only.

Tables are dropped and recreated inside one transaction, so the dashboard never sees a partial load.
"""
import argparse
import csv
import gzip
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

DDL = """
DROP TABLE IF EXISTS reviews, issues, topic_aggregates, trends_monthly, claims, recommendations, run_meta CASCADE;
CREATE TABLE reviews(review_id text PRIMARY KEY, status text NOT NULL, reason text, topic text, subtopic text, intent text,
  severity smallint, sentiment real, needs_review boolean, evidence_quote text, review_rating smallint, review_likes integer,
  review_month text, app_version text, issue_id text, label_source text, cache_source_id text);
CREATE TABLE issues(issue_id text PRIMARY KEY, rank integer, topic text, name text, description text, coherent boolean,
  complaint_count integer, severity_sum integer, mean_severity numeric(10,6), priority_score integer,
  share_of_complaints_pct numeric(6,1), cancellation_count integer, severity_4plus_count integer, needs_review_count integer,
  example_review_ids text[]);
CREATE TABLE topic_aggregates(grp text PRIMARY KEY, data jsonb);
CREATE TABLE trends_monthly(month text PRIMARY KEY, data jsonb);
CREATE TABLE claims(claim_id text PRIMARY KEY, subject text, metric text, value text);
CREATE TABLE recommendations(id serial PRIMARY KEY, kind text, priority_area text, title text, body text, claim_ids text[],
  review_ids text[], model text, check_status text);
CREATE TABLE run_meta(k text PRIMARY KEY, v jsonb);
"""
POST = """
CREATE INDEX reviews_issue ON reviews(issue_id, severity DESC, review_likes DESC);
CREATE INDEX reviews_topic ON reviews(topic, intent);
ANALYZE;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="full")
    a = ap.parse_args()
    load_dotenv(ROOT / ".env")
    api_url = os.environ.get("DATABASE_URL")
    url = os.environ.get("DATABASE_URL_ADMIN") or api_url
    if not url:
        sys.exit("DATABASE_URL_ADMIN (or DATABASE_URL) is not set in .env")
    # Neon: the pooled host (-pooler) suits the serverless API; a bulk COPY uses the direct host
    url = url.replace("-pooler.", ".", 1)
    from urllib.parse import urlparse
    api_role = urlparse(api_url).username if api_url else None
    owner_role = urlparse(url).username
    run_dir = ROOT / "runs" / a.run
    st = sqlite3.connect(ROOT / "data/state" / f"{a.run}.sqlite")
    member = {}
    with open(run_dir / "membership.csv") as f:
        for r in csv.DictReader(f):
            member[r["review_id"]] = r["issue_id"]
    buf = io.StringIO()
    w = csv.writer(buf)
    n = 0
    for r in st.execute("""SELECT c.review_id, status, reason, topic, subtopic, intent, severity, sentiment, needs_review,
                           evidence_quote, review_rating, review_likes, substr(review_timestamp,1,7), app_version, label_source,
                           cache_source_id FROM records c JOIN reviews USING(review_id)"""):
        rid = r[0]
        status = r[1] if r[1] != "pending" else "quarantined"
        w.writerow([rid, status, r[2] or (None if r[1] == "completed" else "unclassified_pending"), r[3], r[4], r[5], r[6],
                    r[7], None if r[8] is None else bool(r[8]), r[9], int(r[10]) if r[10] else None,
                    int(r[11]) if r[11] else 0, r[12], r[13] or None, member.get(rid), r[14], r[15]])
        n += 1
    issues = json.loads((run_dir / "issues.json").read_text())["issues"]
    imeta = {i["issue_id"]: i for i in issues}
    agg = list(csv.DictReader(open(run_dir / "aggregates.csv")))
    topics = list(csv.DictReader(open(run_dir / "topic_aggregates.csv")))
    trends = list(csv.DictReader(open(run_dir / "trends_monthly.csv"))) if (run_dir / "trends_monthly.csv").exists() else []
    claims = list(csv.DictReader(open(run_dir / "claims.csv"))) + list(csv.DictReader(open(run_dir / "topic_claims.csv")))
    memo = json.loads((run_dir / "memo.json").read_text())
    check = json.loads((run_dir / "memo_check.json").read_text())
    meta = {"run_summary": json.loads((run_dir / "run_summary.json").read_text()),
            "verify_report": json.loads((run_dir / "verify" / "verify_report.json").read_text()) if (run_dir / "verify/verify_report.json").exists() else None,
            "trends_comparison": json.loads((run_dir / "trends_comparison.json").read_text()) if (run_dir / "trends_comparison.json").exists() else None,
            "golden_eval": json.loads((ROOT / "evals/golden/golden_eval_summary.json").read_text()) if (ROOT / "evals/golden/golden_eval_summary.json").exists() else None,
            "memo_markdown": (run_dir / "memo.md").read_text(), "memo_check": check}
    import subprocess
    chk = subprocess.run([sys.executable, str(ROOT / "tools" / "check_memo.py"), "--run", a.run], capture_output=True, text=True)
    final_check = "pass" if (chk.returncode == 0 and "problems: none" in chk.stdout) else "fail"
    if (ROOT / "memo.md").exists():
        meta["final_memo_markdown"] = (ROOT / "memo.md").read_text()
        meta["final_memo_check"] = {"status": final_check, "tool": "tools/check_memo.py", "output": chk.stdout.strip()}
    s = meta["run_summary"]
    meta["run_summary"] = {k: s[k] for k in ("run", "label_config", "input_sha256", "records", "quarantine_reasons", "spent_usd_total", "models", "prompt_versions")}
    with psycopg.connect(url) as con:
        with con.transaction():
            con.execute(DDL)
            with con.cursor().copy("COPY reviews FROM STDIN WITH (FORMAT csv)") as cp:
                cp.write(buf.getvalue())
            for x in agg:
                i = imeta.get(x["issue_id"], {})
                con.execute("INSERT INTO issues VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (x["issue_id"], int(x["rank"]), x["topic"], x["name"], i.get("description", ""), i.get("coherent"),
                             int(x["complaint_count"]), int(x["severity_sum"]), x["mean_severity"], int(x["priority_score"]),
                             x["share_of_complaints_pct"], int(x["cancellation_count"]), int(x["severity_4plus_count"]),
                             int(x["needs_review_count"]), i.get("example_review_ids", [])))
            for t in topics:
                con.execute("INSERT INTO topic_aggregates VALUES(%s,%s)", (t["group"], json.dumps(t)))
            for t in trends:
                con.execute("INSERT INTO trends_monthly VALUES(%s,%s)", (t["month"], json.dumps(t)))
            for c in claims:
                con.execute("INSERT INTO claims VALUES(%s,%s,%s,%s)", (c["claim_id"], c.get("issue_id") or c.get("group"), c["metric"], c["value"]))
            con.execute("INSERT INTO recommendations(kind, priority_area, title, body, claim_ids, review_ids, model, check_status) "
                        "VALUES('headline',%s,%s,%s,%s,%s,%s,%s)", (memo["priority_area"], "Recommendation", memo["recommendation"], [], [],
                                                                      "claude-sonnet-5-5", check["status"]))
            for c in memo["cards"]:
                con.execute("INSERT INTO recommendations(kind, priority_area, title, body, claim_ids, review_ids, model, check_status) "
                            "VALUES('card',%s,%s,%s,%s,%s,%s,%s)", (memo["priority_area"], c["title"], c["rationale"], c["claim_ids"],
                                                                    c["review_ids"], "claude-sonnet-5-5", check["status"]))
            fr = ROOT / "final_recommendation.json"
            if fr.exists():   # student-revised final recommendation (the AI original stays in the 'headline'/'card' rows)
                f = json.loads(fr.read_text())
                con.execute("INSERT INTO recommendations(kind, priority_area, title, body, claim_ids, review_ids, model, check_status) "
                            "VALUES('final',%s,%s,%s,%s,%s,%s,%s)", (f["priority_area"], f["title"], f["summary"], f["claim_ids"],
                                                                     f["review_ids"], f["revised_by"], final_check))
            for k, v in meta.items():
                con.execute("INSERT INTO run_meta VALUES(%s,%s)", (k, json.dumps(v)))
            con.execute(POST)
            if api_role and api_role != owner_role:   # least privilege: the deployed API can only read
                con.execute(f'GRANT USAGE ON SCHEMA public TO "{api_role}"')
                con.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{api_role}"')
        cnt = con.execute("SELECT count(*), count(*) FILTER (WHERE status='completed') FROM reviews").fetchone()
    print(f"loaded {n} reviews (db count {cnt[0]}, completed {cnt[1]}), {len(agg)} issues, {len(claims)} claims, "
          f"{len(memo['cards'])} recommendation cards")


if __name__ == "__main__":
    main()

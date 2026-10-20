"""Offline check of memo.md against the saved full-run calculations (no model calls).

    python tools/check_memo.py [--memo memo.md] [--run full]

1. every "<number> [C###|T###]" must equal that claim's value in claims.csv / topic_claims.csv (thousands separators allowed)
2. every other number in the memo prose must appear in the saved evidence (claims, trends comparison, coverage) - listed if not
3. every [R:review_id] must exist in the run, and a quoted phrase next to it must be a substring of that review's text
4. every ISS-... issue id must exist in ranking.csv
"""
import argparse, csv, json, re, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser(); ap.add_argument("--memo", default="memo.md"); ap.add_argument("--run", default="full")
a = ap.parse_args()
run = ROOT / "runs" / a.run
text = (ROOT / a.memo).read_text().split("\n---\n")[0]          # prose only (the generated claims table follows '---')
claims = {r["claim_id"]: r["value"] for f in ("claims.csv", "topic_claims.csv") for r in csv.DictReader(open(run / f))}
issues = {r["issue_id"] for r in csv.DictReader(open(run / "ranking.csv"))}
evidence = json.dumps(json.load(open(run / "memo_evidence_pack.json")))
ev_nums = {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", evidence)}
norm = lambda n: n.replace(",", "").rstrip("%")
problems, ok = [], 0
# a claim tag binds to the nearest number before it in the same clause (up to 60 non-digit characters in between)
cited = re.findall(r"(\d[\d,]*(?:\.\d+)?)(?![\d+])%?(?:[^\[\]\d(]|\d\+){0,60}\[([CT]\d{3})\]", text)   # "severity 4+" is a label, not a value
tags = re.findall(r"\[([CT]\d{3})\]", text)
if len(cited) != len(tags):
    problems.append(f"{len(tags) - len(cited)} claim tag(s) without a number right before them")
for num, cid in cited:
    if cid not in claims:
        problems.append(f"unknown claim {cid}")
    elif norm(num) != claims[cid].rstrip("%") and float(norm(num)) != float(claims[cid]):
        problems.append(f"{num} [{cid}] but saved value is {claims[cid]}")
    else:
        ok += 1
for iid in set(re.findall(r"ISS-[a-z_]+-[a-z_]+", text)):
    if iid not in issues:
        problems.append(f"unknown issue {iid}")
stripped = re.sub(r"\[(?:R:[^\]]+|[CT]\d{3})\]|ISS-[a-z_]+-[a-z_]+", " ", text)
unsourced = sorted({n for n in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])", stripped)
                    if norm(n) not in ev_nums and norm(n) not in {str(i) for i in range(11)} | {"2022", "2023"}})
rids = re.findall(r"\[R:([^\]]+)\]", text)
# review texts: the run database if present, else the raw CSV (README: place it in data/raw/), else IDs only
texts = {}
db = ROOT / "data" / "state" / f"{a.run}.sqlite"
raw = ROOT / "data" / "raw" / "spotify_reviews_18months.csv"
if db.exists():
    con = sqlite3.connect(db)
    for r in rids:
        row = con.execute("SELECT review_text FROM reviews WHERE review_id=?", (r,)).fetchone()
        if row: texts[r] = row[0]
elif raw.exists():
    with open(raw, encoding="utf-8", newline="") as f:
        want = set(rids)
        texts = {r["review_id"]: r["review_text"] for r in csv.DictReader(f) if r["review_id"] in want}
else:
    import gzip
    with gzip.open(ROOT / "grading" / "records.jsonl.gz", "rt") as f:
        known = {json.loads(l)["review_id"] for l in f}
    texts = {r: None for r in rids if r in known}
    print("note: no run database or raw CSV found - checking review IDs only, not quote text")
for line in text.splitlines():
    for q, rid in re.findall(r'"([^"]{8,})"\s*\[R:([^\]]+)\]', line):
        if rid not in texts:
            problems.append(f"review {rid} not found")
            continue
        if texts[rid] is None:
            continue
        parts = [p.strip(" .") for p in q.split("...") if p.strip(" .")]
        if not all(p in texts[rid] for p in parts):
            problems.append(f"quote for {rid} not found verbatim in the review text: {q[:60]}")
missing = [r for r in rids if r not in texts]
print(f"claim tags: {len(tags)}; claim-cited numbers verified: {ok}; review ids cited: {len(rids)} (missing: {len(missing)}); issue ids: {len(set(re.findall(r'ISS-[a-z_]+-[a-z_]+', text)))}")
print("numbers not found in saved evidence:", unsourced or "none")
print("problems:", problems or "none")

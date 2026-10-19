"""Stage 1 - Prepare. Code only, no model calls.

Reads every row of the input CSV with a multiline-safe parser, preserves the six source strings
exactly, computes the contract row hash, profiles quality, finds exact duplicate texts, quarantines
empty texts and queues every other review as pending.
"""
import hashlib
import json
import sys
import time
from collections import Counter

from . import settings

sys.path.insert(0, str(settings.ROOT / "tools"))
import check_submission as cs  # noqa: E402  (course-provided helper: csv_rows, row_sha, profile)


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ingest(store, csv_path, log):
    if store.get_meta("ingested"):
        log(f"ingest: already ingested {store.get_meta('input_path')} - skipping (state is reused)")
        return store.get_meta("ingestion_report")
    t0 = time.time()
    file_sha = cs.sha(csv_path)
    rows, ids, dup_ids = [], set(), 0
    for i, row in enumerate(cs.csv_rows(csv_path)):
        rid = row["review_id"]
        dup_ids += rid in ids
        ids.add(rid)
        rows.append((rid, i, cs.row_sha(row), text_sha(row["review_text"]), row["review_text"], row["review_rating"],
                     row["review_likes"], row["app_version"], row["review_timestamp"], int(not row["review_text"].strip())))
    if dup_ids:
        raise SystemExit(f"ingest: {dup_ids} duplicate review_ids - refusing to continue (IDs must be unique)")
    db = store.db
    db.execute("BEGIN")
    db.executemany("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO records(review_id,status,reason) VALUES(?,?,?)",
                   [(r[0], "quarantined" if r[9] else "pending", "empty_review_text" if r[9] else None) for r in rows])
    db.execute("COMMIT")

    nonempty = [r for r in rows if not r[9]]
    text_counts = Counter(r[3] for r in nonempty)
    ratings = Counter(r[5] for r in rows)
    months = Counter(r[8][:7] for r in rows)
    report = {
        "input_path": str(csv_path), "file_sha256": file_sha, "bytes": settings.Path(csv_path).stat().st_size,
        "records": len(rows), "unique_review_ids": len(ids), "duplicate_review_ids": dup_ids,
        "empty_review_text": len(rows) - len(nonempty), "nonempty_review_text": len(nonempty),
        "missing_app_version": sum(1 for r in rows if not r[7].strip()),
        "distinct_nonempty_texts": len(text_counts),
        "reviews_with_repeated_text": sum(c for c in text_counts.values() if c > 1),
        "texts_repeated": sum(1 for c in text_counts.values() if c > 1),
        "top_repeated_texts": [{"text": next(r[4] for r in nonempty if r[3] == h)[:60], "count": c}
                               for h, c in text_counts.most_common(10)],
        "reviews_by_rating": dict(sorted(ratings.items())), "reviews_by_month": dict(sorted(months.items())),
        "first_review": min(r[8] for r in rows), "last_review": max(r[8] for r in rows),
        "text_length_chars": _quantiles([len(r[4]) for r in nonempty]),
        "quarantined": [{"review_id": r[0], "reason": "empty_review_text"} for r in rows if r[9]],
        "pending_queue": len(nonempty), "seconds": round(time.time() - t0, 2),
        "note": "All source rows retained; empty/whitespace-only texts quarantined with reason empty_review_text; "
                "missing app_version does not block classification.",
    }
    store.set_meta("ingested", True)
    store.set_meta("input_path", str(csv_path))
    store.set_meta("input_sha256", file_sha)
    store.set_meta("ingestion_report", report)
    log(f"ingest: {len(rows)} rows, {len(nonempty)} nonempty, {report['empty_review_text']} empty quarantined, "
        f"{len(text_counts)} distinct texts ({report['seconds']}s)")
    return report


def _quantiles(xs):
    xs = sorted(xs)
    if not xs:
        return {}
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    return {"min": xs[0], "p25": q(.25), "median": q(.5), "p75": q(.75), "p90": q(.9), "p99": q(.99), "max": xs[-1],
            "mean": round(sum(xs) / len(xs), 1)}


def write_profile(csv_path, out_path):
    """The course helper's deterministic profile (grading/ingestion.json)."""
    cs.write_json(out_path, cs.profile(csv_path))

"""Export saved state to inspectable artifacts and the standardized grading/ folder (no model calls)."""
import gzip
import json
import shutil
from pathlib import Path

from . import ingest, settings

ROLES = {"enrich", "verify", "group", "memo"}


def _record(r):
    if r["status"] == "completed":
        out = {"review_id": r["review_id"], "source_sha256": r["source_sha256"], "status": "completed", "topic": r["topic"],
               "intent": r["intent"], "sentiment": float(r["sentiment"]), "severity": int(r["severity"]),
               "entities": json.loads(r["entities"]), "evidence_quote": r["evidence_quote"], "needs_review": bool(r["needs_review"]),
               "label_config": r["label_config"], "subtopic": r["subtopic"], "label_source": r["label_source"]}
        if r["cache_source_id"]:
            out["cache_source_id"] = r["cache_source_id"]
        return out
    # pending rows at export time are not completed classifications: they are exported as quarantined
    # with an explicit reason so every source ID still has exactly one final record
    reason = r["reason"] if r["status"] == "quarantined" else f"unclassified_pending ({r['reason'] or 'not yet attempted'})"
    return {"review_id": r["review_id"], "source_sha256": r["source_sha256"], "status": "quarantined", "reason": reason,
            "attempts": r["attempts"]}


def run(store, prov, opts, run_dir, log):
    run_dir = Path(run_dir)
    g = run_dir / "grading"
    g.mkdir(parents=True, exist_ok=True)
    db = store.db
    q = """SELECT c.*, r.source_sha256, r.row_idx FROM records c JOIN reviews r USING(review_id) ORDER BY r.row_idx"""
    n = {"completed": 0, "quarantined": 0, "pending": 0}
    with gzip.open(g / "records.jsonl.gz", "wt", encoding="utf-8") as rec, \
            gzip.open(run_dir / "enriched.jsonl.gz", "wt", encoding="utf-8") as enr, \
            (run_dir / "quarantine.jsonl").open("w") as qf:
        for r in db.execute(q):
            o = _record(r)
            n[r["status"]] += 1
            rec.write(json.dumps(o, ensure_ascii=False) + "\n")
            if r["status"] == "completed":
                enr.write(json.dumps({**o, "detail": json.loads(r["detail"] or "{}")}, ensure_ascii=False) + "\n")
            else:
                qf.write(json.dumps(o, ensure_ascii=False) + "\n")
    # calls
    seen = set()
    with gzip.open(g / "calls.jsonl.gz", "wt", encoding="utf-8") as cf, (run_dir / "calls_full.jsonl").open("w") as full:
        for c in db.execute("SELECT * FROM calls ORDER BY call_id"):
            rid = c["request_id"]
            k = 1
            while rid in seen:
                k += 1
                rid = f"{c['request_id']}#{k}"
            seen.add(rid)
            ev = {"request_id": rid, "role": c["role"], "review_ids": json.loads(c["review_ids"]), "model": c["model"],
                  "model_resolved": c["model_resolved"], "phase": c["phase"], "outcome": c["outcome"],
                  "label_config": c["label_config"], "input_tokens": int(c["input_tokens"] or 0),
                  "output_tokens": int(c["output_tokens"] or 0), "cache_read_tokens": int(c["cache_read_tokens"] or 0),
                  "cache_write_tokens": int(c["cache_write_tokens"] or 0), "cost_usd": round(c["cost_usd"] or 0, 8),
                  "attempt": c["attempt"], "invocation": c["invocation"], "prompt_version": c["prompt_version"],
                  "error": c["error"], "latency_ms": c["latency_ms"], "batch_key": c["batch_key"]}
            if c["role"] in ROLES:
                cf.write(json.dumps(ev, ensure_ascii=False) + "\n")
            full.write(json.dumps({**ev, "settings": c["settings"], "started_at": c["started_at"]}) + "\n")
    # run.json + ingestion profile
    total = store.count()
    (g / "run.json").write_text(json.dumps({"version": "a5-audit-v1", "analysis_count": total,
                                            "analysis_sha256": store.get_meta("input_sha256"),
                                            "classification_input_fields": ["review_text"], "allow_multi_issue": False,
                                            "label_config": settings.LABEL_CONFIG, "run_dir": str(run_dir.relative_to(settings.ROOT))},
                                           indent=1))
    prof = g / "ingestion.json"
    if not prof.exists() or json.loads(prof.read_text()).get("file_sha256") != store.get_meta("input_sha256"):
        ingest.write_profile(store.get_meta("input_path"), prof)
    for name in ("membership.csv", "ranking.csv", "claims.csv"):
        if (run_dir / name).exists():
            shutil.copy(run_dir / name, g / name)
    # checkpoints: snapshot at the first interrupted/stopped invocation, and the latest end snapshot
    invs = [dict(r) for r in db.execute("SELECT * FROM invocations ORDER BY invocation")]
    stopped = [i for i in invs if i["status"] and (i["status"].startswith("interrupted") or i["status"].startswith("stopped"))]
    cp = run_dir / "checkpoints"
    if stopped and (cp / f"inv{stopped[0]['invocation']}_end.json").exists():
        before = json.loads((cp / f"inv{stopped[0]['invocation']}_end.json").read_text())
        (g / "checkpoint_before.json").write_text(json.dumps({"invocation": stopped[0]["invocation"], "taken_at": before["taken_at"],
                                                              "completed_ids": before["completed_ids"]}))
        after_ids = store.completed_ids()
        (g / "checkpoint_after.json").write_text(json.dumps({"invocation": invs[-1]["invocation"], "completed_ids": after_ids}))
    else:
        log("export: no interrupted invocation found - checkpoint_before/after not written")
    _write_manifest(store, run_dir, log)
    log(f"export: {n} -> {g}")


COURSE_EXPECTED = {"records": 660622, "empty_review_text": 13, "missing_app_version": 159701, "duplicate_review_ids": 0,
                   "distinct_nonempty_texts": 484189,
                   "file_sha256": "1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6"}


def _sha(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_manifest(store, run_dir, log):
    """ingestion_report.json (full-file profile + check against the course's published counts) and
    data_manifest.json (ties source checksum, code version, prompts, models, settings and outputs together)."""
    import subprocess
    rep = dict(store.get_meta("ingestion_report") or {})
    if rep.get("file_sha256") == COURSE_EXPECTED["file_sha256"]:
        rep["course_expected_check"] = {k: {"expected": v, "observed": rep.get(k), "match": rep.get(k) == v}
                                        for k, v in COURSE_EXPECTED.items()}
    (run_dir / "ingestion_report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False))

    def git(*a):
        try:
            return subprocess.run(["git", *a], cwd=settings.ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            return None
    dirty = git("status", "--porcelain", "--", "pipeline", "prompts", "labels", "cost/calculator.py")
    outputs = {}
    for f in sorted(run_dir.rglob("*")):
        if f.is_file() and f.name != "data_manifest.json" and "checkpoints" not in f.parts:
            outputs[str(f.relative_to(run_dir))] = {"bytes": f.stat().st_size, "sha256": _sha(f)}
    inv = [dict(r) for r in store.db.execute("SELECT invocation, phase, status, started_at, ended_at, completed_before, completed_after FROM invocations")]
    man = {"run_id": run_dir.name, "source": {"path": store.get_meta("input_path"), "sha256": store.get_meta("input_sha256"),
                                              "rows": store.count()},
           "code_version": {"git_commit": git("rev-parse", "HEAD"), "uncommitted_changes_in_pipeline_code": bool(dirty)},
           "label_config": settings.LABEL_CONFIG,
           "models": {"enrich": settings.HAIKU, "verify": settings.JEV_MODEL, "group": settings.HAIKU, "memo": settings.SONNET},
           "resolved_models": sorted({r[0] for r in store.db.execute("SELECT DISTINCT model_resolved FROM calls WHERE model_resolved IS NOT NULL")}),
           "prompts": {f.name: _sha(f) for f in sorted(settings.PROMPTS.glob("*")) if f.is_file()},
           "labels": {f.name: _sha(f) for f in sorted(settings.LABELS.glob("*.json"))},
           "defaults": settings.DEFAULTS, "provider_caps_usd": settings.PROVIDER_CAPS_USD,
           "invocations": inv, "outputs": outputs}
    (run_dir / "data_manifest.json").write_text(json.dumps(man, indent=1, default=str))
    log(f"export: wrote ingestion_report.json and data_manifest.json (git {str(man['code_version']['git_commit'])[:8]}, "
        f"uncommitted pipeline changes: {man['code_version']['uncommitted_changes_in_pipeline_code']})")

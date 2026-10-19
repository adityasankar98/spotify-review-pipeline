"""Orchestrator CLI.  python -m pipeline <command> ...

  run     --input CSV --run NAME [--stages ...] [controls]   staged pipeline (paid model calls)
  rank    --run NAME | --records F --membership F --out DIR  deterministic ranking, no model calls
  export  --run NAME                                          write grading/ export from saved state
  status  --run NAME                                          counts, spend, invocations

The orchestrator owns dispatch, state, validation, budgets and logs; models only read language.
Each stage reads the previous stage's saved handoff and writes its own, so any stage can be rerun.
"""
import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from . import settings
from .store import Store

STAGES = ["ingest", "enrich", "verify", "group", "rank", "recommend", "export"]


class RunLog:
    def __init__(self, run_dir):
        self.path = Path(run_dir) / "run_log.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg, **fields):
        rec = {"t": round(time.time(), 3), "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": msg, **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{rec['ts']}] {msg}", flush=True)


def paths(run):
    return settings.STATE_DIR / f"{run}.sqlite", settings.RUNS_DIR / run


def snapshot(store, run_dir, name):
    d = run_dir / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    ids = store.completed_ids()
    tmp = d / f"{name}.json.tmp"
    tmp.write_text(json.dumps({"snapshot": name, "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "completed": len(ids), "pending": store.count("pending"),
                               "quarantined": store.count("quarantined"), "completed_ids": ids}))
    tmp.replace(d / f"{name}.json")
    return len(ids)


def cmd_run(a):
    load_dotenv(settings.ROOT / ".env")
    db_path, run_dir = paths(a.run)
    store = Store(db_path)
    log = RunLog(run_dir)
    opts = dict(settings.DEFAULTS)
    for k in opts:
        v = getattr(a, k, None)
        if v is not None:
            opts[k] = v
    stages = a.stages.split(",") if a.stages else STAGES
    inv, phase = store.start_invocation(sys.argv)
    log(f"=== invocation {inv} ({phase}) run={a.run} stages={stages} opts={opts}", invocation=inv, phase=phase)
    status = "completed"
    from . import ingest as ingest_mod
    t0 = time.time()
    timings = {}
    try:
        if "ingest" in stages:
            s = time.time()
            ingest_mod.ingest(store, a.input, log)
            timings["ingest"] = time.time() - s
        snapshot(store, run_dir, f"inv{inv}_start")
        status = asyncio.run(_model_stages(store, opts, stages, inv, phase, log, run_dir, timings))
    except KeyboardInterrupt:
        status = "interrupted_hard"
        if store.db.in_transaction:
            store.db.execute("ROLLBACK")
        log("hard interrupt: uncommitted batch rolled back; committed batches are kept")
    finally:
        n = snapshot(store, run_dir, f"inv{inv}_end")
        store.end_invocation(inv, status)
        timings["wall_clock_total"] = time.time() - t0
        _write_summary(store, run_dir, a.run, log, timings, opts)
        log(f"=== invocation {inv} ended: {status}; completed={n} pending={store.count('pending')} "
            f"quarantined={store.count('quarantined')} spent=${store.spent_usd():.4f} wall={timings['wall_clock_total']:.1f}s")
    return 0 if status == "completed" else 2


async def _model_stages(store, opts, stages, inv, phase, log, run_dir, timings):
    from . import enrich, group, rank, recommend, verify, export
    from .providers import Ledger, Providers
    ledger = Ledger(store, opts["budget_usd"], opts["max_minutes"])
    prov = Providers(store, ledger, settings.load_rates(), opts, inv, phase, log)
    stop = {"flag": False}
    loop = asyncio.get_running_loop()

    def request_stop():
        if stop["flag"]:
            raise KeyboardInterrupt
        stop["flag"] = True
        ledger.deadline = time.time() - 1  # stop admitting new work; in-flight batches finish and commit
        ledger.stop_reason = "interrupted"
        log("SIGINT: graceful stop requested - finishing in-flight batches (press Ctrl-C again to abort)")

    loop.add_signal_handler(signal.SIGINT, request_stop)
    loop.add_signal_handler(signal.SIGTERM, request_stop)
    status = "completed"
    try:
        if "enrich" in stages:
            s = time.time()
            st = await enrich.run(store, prov, opts, inv, log)
            timings["enrich"] = time.time() - s
            if st["stop"]:
                return "interrupted" if stop["flag"] else f"stopped_{st['stop']}"
            if store.count("pending"):
                log(f"enrich: {store.count('pending')} records still pending - downstream stages use completed records only")
        for name, fn in (("verify", verify.run), ("group", group.run), ("rank", rank.run_from_store),
                         ("recommend", recommend.run), ("export", export.run)):
            if name in stages:
                s = time.time()
                r = fn(store, prov, opts, run_dir, log)
                if asyncio.iscoroutine(r):
                    await r
                timings[name] = time.time() - s
    finally:
        await prov.close()
    return status


def _write_summary(store, run_dir, run, log, timings, opts):
    db = store.db
    calls = db.execute("""SELECT role, model, outcome, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o,
                          SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw, SUM(cost_usd) c
                          FROM calls GROUP BY role, model, outcome""").fetchall()
    invs = [dict(r) for r in db.execute("SELECT * FROM invocations ORDER BY invocation")]
    by_source = {r[0] or "none": r[1] for r in db.execute(
        "SELECT label_source, COUNT(*) FROM records WHERE status='completed' AND cache_source_id IS NULL GROUP BY label_source")}
    summary = {
        "run": run, "label_config": settings.LABEL_CONFIG, "input": store.get_meta("input_path"),
        "input_sha256": store.get_meta("input_sha256"),
        "records": {"total": store.count(), "completed": store.count("completed"), "pending": store.count("pending"),
                    "quarantined": store.count("quarantined"),
                    "completed_via_cache": db.execute("SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL").fetchone()[0],
                    "completed_origin_by_source": by_source,
                    "needs_review": db.execute("SELECT COUNT(*) FROM records WHERE needs_review=1").fetchone()[0]},
        "quarantine_reasons": {r[0]: r[1] for r in db.execute(
            "SELECT reason, COUNT(*) FROM records WHERE status='quarantined' GROUP BY reason")},
        "calls": [dict(r) for r in calls], "spent_usd_total": round(store.spent_usd(), 6),
        "controls": opts, "last_invocation_timings_s": {k: round(v, 2) for k, v in timings.items()},
        "invocations": invs,
        "models": {"enrich_primary": settings.JEV_MODEL, "enrich_fallback": settings.HAIKU, "verify": settings.HAIKU,
                   "group": settings.HAIKU, "memo": settings.SONNET},
        "prompt_versions": {"jev": settings.JEV_QUESTIONS_VERSION, "fallback": settings.FALLBACK_PROMPT_VERSION,
                            "verify": settings.VERIFY_PROMPT_VERSION, "group": settings.GROUP_PROMPT_VERSION,
                            "memo": settings.MEMO_PROMPT_VERSION, "schema": settings.SCHEMA_VERSION,
                            "extract": settings.EXTRACT_VERSION},
    }
    (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))


def cmd_rank(a):
    from . import rank
    if a.run:
        db_path, run_dir = paths(a.run)
        rank.run_from_store(Store(db_path), None, None, run_dir, print)
    else:
        rank.run_from_files(a.records, a.membership, Path(a.out), print)


def cmd_export(a):
    from . import export
    db_path, run_dir = paths(a.run)
    export.run(Store(db_path), None, None, run_dir, print)


def cmd_status(a):
    db_path, run_dir = paths(a.run)
    s = Store(db_path)
    print(json.dumps({"completed": s.count("completed"), "pending": s.count("pending"), "quarantined": s.count("quarantined"),
                      "spent_usd": round(s.spent_usd(), 4),
                      "calls": s.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
                      "invocations": [dict(r) for r in s.db.execute(
                          "SELECT invocation, phase, status, completed_before, completed_after FROM invocations")]}, indent=1))


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--input", required=True)
    r.add_argument("--run", required=True)
    r.add_argument("--stages")
    for k, v in settings.DEFAULTS.items():
        r.add_argument("--" + k.replace("_", "-"), dest=k, type=type(v))
    r.set_defaults(fn=cmd_run)
    k = sub.add_parser("rank")
    k.add_argument("--run")
    k.add_argument("--records")
    k.add_argument("--membership")
    k.add_argument("--out", default="ranking_rebuild")
    k.set_defaults(fn=cmd_rank)
    e = sub.add_parser("export")
    e.add_argument("--run", required=True)
    e.set_defaults(fn=cmd_export)
    st = sub.add_parser("status")
    st.add_argument("--run", required=True)
    st.set_defaults(fn=cmd_status)
    a = p.parse_args(argv)
    sys.exit(a.fn(a) or 0)

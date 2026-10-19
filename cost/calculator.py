"""100-review cost and runtime calculator.

Default = OFFLINE REPLAY (no API key, no network, no model calls):
    python cost/calculator.py                         # recompute report from saved usage + rates.csv
    python cost/calculator.py --rates my_rates.csv --budget 80 --workers 4 --fallback-cap 0.10

Explicit PAID pilot (separate command; spends real money; needs .env keys):
    python cost/calculator.py pilot --confirm-paid    # cold run on cost_100.csv with an empty cache, 1 worker,
                                                      # then a warm rerun with the saved cache

Refresh projections from larger development checkpoints (offline, reads saved call logs):
    python cost/calculator.py --calibrate runs/dev500 runs/dev10k

Arithmetic: item_cost = billed_units x price_per_unit (price per 1M tokens / 1,000,000). Input,
cache-read, cache-write and output tokens are mutually exclusive provider usage fields; thinking is
already inside output tokens and is not added again. Local compute is reported as UNKNOWN, not zero.
"""
import argparse
import csv
import gzip
import hashlib
import json
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PILOT_INPUT = ROOT / "data/raw/cost_100.csv"
PILOT_SHA = "c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e"
ITEMS = ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens")
FULL = {"rows": 660622, "nonempty": 660609, "empty_quarantined": 13, "distinct_texts": 484189}


# ---------------------------------------------------------------- rates / usage
def load_rates(path):
    rates, meta = {}, {}
    with open(path) as f:
        for r in csv.DictReader(f):
            rates[(r["model"], r["item"])] = float(r["usd_per_unit"]) / float(r["unit_size"])
            meta[(r["model"], r["item"])] = r
    return rates, meta


def rate_key(model):
    return "jev-latest" if model.startswith("jev") else model


def stage_of(c):
    if c["role"] == "enrich":
        return "enrich:jev" if c["model"].startswith("jev") else "enrich:fallback"
    return c["role"]


def call_cost(c, rates):
    return {it: (c.get(it) or 0) * rates.get((rate_key(c["model"]), it), 0.0) for it in ITEMS}


def read_jsonl(p):
    p = Path(p)
    op = gzip.open if p.suffix == ".gz" else open
    with op(p, "rt", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------- measured summary
def summarize(calls, rates, run_label):
    st = defaultdict(lambda: {"requests": 0, "attempts": 0, "failed_attempts": 0, "review_slots": 0, "latency_ms": [],
                              "usage": defaultdict(int), "cost": defaultdict(float), "models": set(), "settings": set(),
                              "prompt_versions": set()})
    for c in calls:
        if c.get("run_label") != run_label:
            continue
        s = st[stage_of(c)]
        s["attempts"] += 1
        s["models"].add(c.get("model_resolved") or c["model"])
        s["settings"].add(c.get("settings") or "")
        s["prompt_versions"].add(c.get("prompt_version") or "")
        if c["outcome"] == "succeeded":
            s["requests"] += 1
            s["review_slots"] += len(c["review_ids"])
        else:
            s["failed_attempts"] += 1
        if c.get("latency_ms") is not None:
            s["latency_ms"].append(c["latency_ms"])
        for it in ITEMS:
            s["usage"][it] += c.get(it) or 0
        for it, v in call_cost(c, rates).items():
            s["cost"][it] += v
    return st


def fmt_usd(x):
    return f"${x:,.4f}" if x < 100 else f"${x:,.2f}"


# ---------------------------------------------------------------- projection
def project(m, args, rates):
    """m: measured unit rates. Returns base and conservative full-run scenarios."""
    out = {}
    for name, k in (("base", 1.0), ("conservative", 1.0)):
        cons = name == "conservative"
        retry = max(m["retry_rate"], 0.02) if not cons else max(3 * m["retry_rate"], 0.10)
        fb_rate = min(m["fallback_rate"], args.fallback_cap) if not cons else args.fallback_cap
        for reuse in (True, False):
            texts = FULL["distinct_texts"] if reuse else FULL["nonempty"]
            jev_calls = texts * (1 + retry)
            jev_cost = jev_calls * m["jev_cost_per_call"]
            fb_reviews = texts * fb_rate
            fb_cost = fb_reviews * m["fallback_cost_per_review"] * (1 + retry)
            ver_cost = args.verify_sample * m["verify_cost_per_review"] * (1 + retry)
            fixed = m["group_cost"] + m["memo_cost"]
            total = jev_cost + fb_cost + ver_cost + fixed
            rps = min(args.jev_rps, args.jev_concurrency / max(m["jev_latency_s"], 1e-3))
            if cons:
                rps *= 0.7
            jev_hours = jev_calls / rps / 3600
            fb_hours = (fb_reviews / 50) * m["fallback_latency_s"] / max(1, args.workers) / 3600
            fixed_hours = (m["ingest_full_s"] + m["group_s"] + m["memo_s"] + args.verify_sample / 50 * m["verify_latency_s"]) / 3600
            out[f"{name}{'' if reuse else '_no_reuse'}"] = {
                "texts_classified": texts, "retry_rate": round(retry, 4), "fallback_rate": round(fb_rate, 4),
                "jev_calls": round(jev_calls), "fallback_requests": round(fb_reviews / 50 * (1 + retry)),
                "cost_usd": {"enrich:jev": jev_cost, "enrich:fallback": fb_cost, "verify": ver_cost, "group+memo (once)": fixed,
                             "total": total},
                "effective_jev_rps": round(rps, 2),
                "hours": {"enrich:jev": jev_hours, "enrich:fallback (overlapping workers)": fb_hours, "fixed": fixed_hours,
                          "total": jev_hours + fb_hours + fixed_hours},
                "over_budget": total > args.budget}
    return out


def _mean_s(s, default):
    return statistics.mean(s["latency_ms"]) / 1000 if s and s["latency_ms"] else default


def measured_rates(st_cold, meas):
    def per(stage, key):
        s = st_cold.get(stage)
        return s if s else None
    j = st_cold.get("enrich:jev")
    fb = st_cold.get("enrich:fallback")
    v = st_cold.get("verify")
    total_attempts = sum(s["attempts"] for s in st_cold.values())
    total_failed = sum(s["failed_attempts"] for s in st_cold.values())
    tot = lambda s: sum(s["cost"].values()) if s else 0.0
    return {
        "jev_cost_per_call": tot(j) / max(1, j["requests"]) if j else 0.0,
        "jev_latency_s": _mean_s(j, 1.0),
        "fallback_rate": (fb["review_slots"] / max(1, meas["unique_texts"])) if fb else 0.0,
        "fallback_cost_per_review": tot(fb) / max(1, fb["review_slots"]) if fb else 0.0,
        "fallback_latency_s": _mean_s(fb, 10.0),
        "verify_cost_per_review": tot(v) / max(1, v["review_slots"]) if v else 0.0,
        "verify_latency_s": _mean_s(v, 10.0),
        "group_cost": tot(st_cold.get("group")), "memo_cost": tot(st_cold.get("memo")),
        "group_s": _mean_s(st_cold.get("group"), 0.0),
        "memo_s": _mean_s(st_cold.get("memo"), 0.0),
        "retry_rate": total_failed / max(1, total_attempts - total_failed),
        "ingest_full_s": meas.get("ingest_full_seconds", 10.0),
    }


# ---------------------------------------------------------------- report
def replay(args):
    rates, rmeta = load_rates(args.rates)
    calls = read_jsonl(HERE / "pilot_calls.jsonl")
    meas = json.loads((HERE / "measurements.json").read_text())
    records = read_jsonl(HERE / "pilot_records.jsonl")
    cold = summarize(calls, rates, "cold")
    warm = summarize(calls, rates, "warm")
    m = measured_rates(cold, meas)
    proj = project(m, args, rates)
    # usage.csv: one row per call x billing item, so every charge maps to a logged request
    with (HERE / "usage.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_label", "request_id", "stage", "model", "item", "units", "usd_per_unit", "cost_usd"])
        for c in calls:
            for it, v in call_cost(c, rates).items():
                if c.get(it):
                    w.writerow([c["run_label"], c["request_id"], stage_of(c), c["model"], it, c[it],
                                f"{rates.get((rate_key(c['model']), it), 0):.12f}", f"{v:.10f}"])
    api_cold = sum(sum(s["cost"].values()) for s in cold.values())
    api_warm = sum(sum(s["cost"].values()) for s in warm.values())
    completed = sum(1 for r in records if r["status"] == "completed")
    L = []
    L.append("# 100-review cost and runtime calculator — report\n")
    L.append(f"Generated offline by `python cost/calculator.py` at {time.strftime('%Y-%m-%d %H:%M')} from saved usage "
             f"(`pilot_calls.jsonl`) and editable rates (`{Path(args.rates).name}`). No API calls were made to produce this report.\n")
    L.append("## Measured pilot (real calls)\n")
    L.append(f"- Input: `cost_100.csv` sha256 `{meas['input_sha256']}` ({'matches manifest' if meas['input_sha256'] == PILOT_SHA else 'MISMATCH'}); "
             f"{meas['ids']} IDs; {meas['unique_texts']} unique texts")
    L.append(f"- Cold run (empty result cache, {meas['cold']['workers']} worker): **{meas['cold']['wall_clock_s']:.1f} s wall-clock**, "
             f"completed {meas['cold']['completed']}, quarantined {meas['cold']['quarantined']}, pending {meas['cold']['pending']}, "
             f"exact-text cache hits {meas['cold']['cache_hits']}")
    L.append(f"- Warm run (saved cache, same settings): **{meas['warm']['wall_clock_s']:.1f} s wall-clock**, "
             f"new enrichment calls: **{meas['warm']['new_enrich_calls']}**, other new calls: {meas['warm']['new_other_calls']}")
    L.append(f"- Verification sample declared: {meas['verify_sample']} of {meas['ids']} records; fallback cap {meas['fallback_cap']}")
    L.append(f"- API spend: cold **{fmt_usd(api_cold)}**, warm incremental **{fmt_usd(api_warm)}**; "
             f"per 1,000 input rows {fmt_usd(api_cold / meas['ids'] * 1000)}; per completed record {fmt_usd(api_cold / max(1, completed))}; "
             f"cold throughput {meas['ids'] / meas['cold']['wall_clock_s']:.2f} rows/s")
    L.append("- Local compute (MacBook Air CPU, SQLite): **unknown / not metered** — not counted as $0 API spend.\n")
    L.append("### By stage (cold run)\n")
    L.append("| stage | provider / model (resolved) | settings | prompt | batch size | requests ok | attempts | failed | review slots | input | cache write | cache read | output | cost |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, s in sorted(cold.items()):
        u = s["usage"]
        bs = "1 review/request" if name == "enrich:jev" else ("<=50 reviews/request" if name in ("enrich:fallback", "verify") else "aggregate pack")
        L.append(f"| {name} | {', '.join(sorted(s['models']))} | {'; '.join(sorted(x for x in s['settings'] if x))[:80]} | "
                 f"{', '.join(sorted(s['prompt_versions']))} | {bs} | {s['requests']} | {s['attempts']} | {s['failed_attempts']} | "
                 f"{s['review_slots']} | {u['input_tokens']:,} | {u['cache_write_tokens']:,} | {u['cache_read_tokens']:,} | "
                 f"{u['output_tokens']:,} | {fmt_usd(sum(s['cost'].values()))} |")
    L.append(f"\nStage wall-clock (cold): " + ", ".join(f"{k} {v:.1f}s" for k, v in meas["cold"]["stage_seconds"].items()))
    L.append("\n### Rates used (editable `rates.csv`)\n")
    L.append("| model | item | USD per unit | unit | tier | source | checked |")
    L.append("|---|---|---|---|---|---|---|")
    for (mdl, it), r in rmeta.items():
        L.append(f"| {mdl} | {it} | {float(r['usd_per_unit']) / float(r['unit_size']):.10f} | {r['unit']} | {r['tier']} | {r['source']} | {r['checked_date']} |")
    L.append("\n## Full-run projection (estimates, not measurements)\n")
    L.append(f"Scope: {FULL['rows']:,} rows → {FULL['nonempty']:,} nonempty classifications + {FULL['empty_quarantined']} empty-text "
             f"quarantines; exact-text reuse reduces first-pass work to {FULL['distinct_texts']:,} distinct texts.\n")
    L.append(f"Controls: budget **${args.budget:,.2f}**, workers {args.workers}, Jev rate limit {args.jev_rps} req/s, Jev concurrency "
             f"{args.jev_concurrency}, fallback cap {args.fallback_cap:.0%}, fallback output-token cap {args.output_token_cap} per request, "
             f"verification sample {args.verify_sample:,}.\n")
    L.append("Measured unit rates from the cold pilot: " + ", ".join(f"{k}={v:.6g}" for k, v in m.items()) + "\n")
    L.append("| scenario | texts | retry | fallback | Jev calls | fallback reqs | Jev $ | fallback $ | verify $ | group+memo $ | **total $** | Jev req/s | hours | budget |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k, p in proj.items():
        c, h = p["cost_usd"], p["hours"]
        L.append(f"| {k} | {p['texts_classified']:,} | {p['retry_rate']:.0%} | {p['fallback_rate']:.1%} | {p['jev_calls']:,} | "
                 f"{p['fallback_requests']:,} | {c['enrich:jev']:.2f} | {c['enrich:fallback']:.2f} | {c['verify']:.2f} | "
                 f"{c['group+memo (once)']:.2f} | **{c['total']:.2f}** | {p['effective_jev_rps']} | {h['total']:.1f} | "
                 f"{'⚠️ EXCEEDS BUDGET' if p['over_budget'] else 'within'} |")
    L.append("\nAssumptions: Jev cost per call and fallback/verify cost per review are the cold-pilot means; one group and one memo "
             "call are fixed overhead counted once (never multiplied by review count); base retry = max(measured, 2%), "
             "conservative retry = max(3× measured, 10%); conservative fallback = the cap; conservative throughput = 70% of "
             "min(rate limit, concurrency / mean Jev latency). Summed call latencies are not used as wall-clock time.\n")
    if args.calibrate:
        L.append("## Refresh from development checkpoints\n")
        L.append("| run | texts (origin) | Jev calls | Jev $/call | fallback share | fallback $/review | verify $/review | retry rate | all-stage $ | wall-clock enrich s | texts/s |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for rd in args.calibrate:
            rd = Path(rd)
            cs = read_jsonl(rd / "calls_full.jsonl")
            for c in cs:
                c["run_label"] = "x"
            s = summarize(cs, rates, "x")
            summ = json.loads((rd / "run_summary.json").read_text())
            origins = sum(summ["records"]["completed_origin_by_source"].values())
            mm = measured_rates(s, {"unique_texts": origins})
            enrich_s = sum(i.get("enrich", 0) for i in [summ.get("last_invocation_timings_s", {})])
            allc = sum(sum(x["cost"].values()) for x in s.values())
            L.append(f"| {rd.name} | {origins:,} | {s['enrich:jev']['requests'] if 'enrich:jev' in s else 0:,} | {mm['jev_cost_per_call']:.6f} | "
                     f"{mm['fallback_rate']:.1%} | {mm['fallback_cost_per_review']:.6f} | {mm['verify_cost_per_review']:.6f} | "
                     f"{mm['retry_rate']:.2%} | {allc:.4f} | {enrich_s:.0f} | {origins / enrich_s if enrich_s else 0:.1f} |")
    L.append("\n## Reproduce\n\n- Offline replay (default, no key): `python cost/calculator.py`\n"
             "- Edit prices: change `cost/rates.csv` (or pass `--rates`), rerun replay. With usage fixed, doubling every API rate doubles API spend; measured times are unchanged.\n"
             "- Paid pilot (explicit): `python cost/calculator.py pilot --confirm-paid`\n")
    report = "\n".join(L) + "\n"
    (HERE / "report.md").write_text(report)
    (HERE / "report.html").write_text(_html(report))
    (HERE / "projection.json").write_text(json.dumps({"measured_unit_rates": m, "scenarios": proj, "controls": vars(args)}, indent=1, default=str))
    print(report)
    return api_cold


def _html(md):
    import html
    rows, out, in_table = md.splitlines(), [], False
    for line in rows:
        if line.startswith("|"):
            cells = [html.escape(c.strip()) for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{c.replace('**', '')}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("#"):
            n = len(line) - len(line.lstrip("#"))
            out.append(f"<h{n}>{html.escape(line.lstrip('# '))}</h{n}>")
        elif line.strip():
            out.append(f"<p>{html.escape(line).replace('**', '')}</p>")
    return ("<!doctype html><meta charset=utf-8><title>Cost Calculator Report</title><style>body{font:14px system-ui;max-width:1200px;"
            "margin:24px auto;padding:0 16px}table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #ccc;padding:3px 6px}"
            "</style>" + "\n".join(out))


# ---------------------------------------------------------------- paid pilot
def pilot(args):
    if not args.confirm_paid:
        sys.exit("Refusing to make paid calls without --confirm-paid. Default mode is offline replay.")
    sha = hashlib.sha256(PILOT_INPUT.read_bytes()).hexdigest()
    if sha != PILOT_SHA:
        sys.exit("cost_100.csv checksum mismatch")
    run = "pilot100"
    for p in (ROOT / f"data/state/{run}.sqlite", ROOT / f"data/state/{run}.sqlite-wal", ROOT / f"data/state/{run}.sqlite-shm"):
        p.unlink(missing_ok=True)          # empty result cache for the cold run
    shutil.rmtree(ROOT / "runs" / run, ignore_errors=True)
    base = [sys.executable, "-m", "pipeline", "run", "--input", str(PILOT_INPUT), "--run", run, "--workers", "1",
            "--verify-sample", str(args.pilot_verify), "--budget-usd", str(args.pilot_budget),
            "--max-fallback-fraction", str(args.fallback_cap)]
    meas = {"input_sha256": sha, "ids": 100, "verify_sample": args.pilot_verify, "fallback_cap": args.fallback_cap}
    for label in ("cold", "warm"):
        t0 = time.time()
        rc = subprocess.run(base, cwd=ROOT).returncode
        wall = time.time() - t0
        summ = json.loads((ROOT / "runs" / run / "run_summary.json").read_text())
        meas[label] = {"wall_clock_s": round(wall, 2), "exit_code": rc, "workers": 1,
                       "completed": summ["records"]["completed"], "quarantined": summ["records"]["quarantined"],
                       "pending": summ["records"]["pending"], "cache_hits": summ["records"]["completed_via_cache"],
                       "stage_seconds": summ["last_invocation_timings_s"]}
    calls = read_jsonl(ROOT / "runs" / run / "calls_full.jsonl")
    for c in calls:
        c["run_label"] = "cold" if c["invocation"] == 1 else "warm"
    meas["warm"]["new_enrich_calls"] = sum(1 for c in calls if c["run_label"] == "warm" and c["role"] == "enrich")
    meas["warm"]["new_other_calls"] = sum(1 for c in calls if c["run_label"] == "warm" and c["role"] != "enrich")
    full_ingest = ROOT / "runs/full/run_summary.json"
    meas["ingest_full_seconds"] = json.loads(full_ingest.read_text())["last_invocation_timings_s"].get("ingest", 10.0) if full_ingest.exists() else 10.0
    recs = read_jsonl(ROOT / "runs" / run / "grading" / "records.jsonl.gz")
    meas["unique_texts"] = _unique_texts()
    (HERE / "pilot_calls.jsonl").write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in calls) + "\n")
    (HERE / "pilot_records.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n")
    (HERE / "measurements.json").write_text(json.dumps(meas, indent=1))
    print(json.dumps(meas, indent=1))


def _unique_texts():
    sys.path.insert(0, str(ROOT / "tools"))
    import check_submission as cs
    return len({r["review_text"] for r in cs.csv_rows(PILOT_INPUT) if r["review_text"].strip()})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", nargs="?", default="replay", choices=["replay", "pilot"])
    p.add_argument("--rates", default=str(HERE / "rates.csv"))
    p.add_argument("--budget", type=float, default=10.0, help="spending limit for the full run (USD)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--jev-rps", type=float, default=20.0)
    p.add_argument("--jev-concurrency", type=int, default=32)
    p.add_argument("--fallback-cap", type=float, default=0.03, help="maximum fraction of texts routed to the fallback")
    p.add_argument("--output-token-cap", type=int, default=6000, help="max output tokens per fallback request")
    p.add_argument("--verify-sample", type=int, default=1000, help="declared full-run verification sample")
    p.add_argument("--calibrate", nargs="*", help="run dirs (e.g. runs/dev500 runs/dev10k) to refresh unit rates")
    p.add_argument("--confirm-paid", action="store_true")
    p.add_argument("--pilot-verify", type=int, default=20)
    p.add_argument("--pilot-budget", type=float, default=0.25)
    a = p.parse_args()
    if a.mode == "pilot":
        pilot(a)
    replay(a)


if __name__ == "__main__":
    main()

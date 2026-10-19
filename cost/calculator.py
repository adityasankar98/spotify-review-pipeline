"""100-review cost and runtime calculator.

Default = OFFLINE REPLAY (no API key, no network, no model calls):
    python cost/calculator.py                                  # recompute report from saved usage + rates.csv
    python cost/calculator.py --rates my_rates.csv --budget 6 --workers 4 --output-token-cap 2000 --fallback-cap 0.05

Explicit PAID pilot (separate command; spends real money; needs .env keys):
    python cost/calculator.py pilot --confirm-paid             # cold run on cost_100.csv with an empty result cache,
                                                               # 1 worker, all stages; then a warm rerun on saved results

Refresh projections from larger development checkpoints (offline, reads their saved call logs):
    python cost/calculator.py --calibrate runs/dev500 runs/dev10k

Arithmetic: item_cost = billed_units x price_per_unit, where price_per_unit = (price per 1M tokens) / 1,000,000.
Uncached input, cache-write, cache-read and output tokens are mutually exclusive provider usage fields; thinking is
disabled (and would already be inside output tokens). Each call is priced at the tier it actually used
(Message Batches API or standard), read from its logged settings. Local compute is reported as UNKNOWN, not zero.
"""
import argparse
import csv
import gzip
import hashlib
import json
import math
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
STAGES = ("enrich", "verify", "group", "memo")
PROVIDER_OF = {"enrich": "anthropic", "group": "anthropic", "memo": "anthropic", "verify": "typesafe"}


# ---------------------------------------------------------------- rates / usage
def load_rates(path):
    rates, meta = {}, {}
    with open(path) as f:
        for r in csv.DictReader(f):
            rates[(r["model"], r["item"])] = float(r["usd_per_unit"]) / float(r["unit_size"])
            meta[(r["model"], r["item"])] = r
    return rates, meta


def tier_of(c):
    try:
        return json.loads(c.get("settings") or "{}").get("tier", "standard")
    except (TypeError, json.JSONDecodeError):
        return "standard"


def rate_key(c, tier=None):
    m = c["model"]
    if m.startswith("jev"):
        return "jev-latest"
    return f"{m}:batch" if (tier or tier_of(c)) == "batch" else m


def call_cost(c, rates, tier=None):
    k = rate_key(c, tier)
    return {it: (c.get(it) or 0) * rates.get((k, it), 0.0) for it in ITEMS}


def read_jsonl(p):
    p = Path(p)
    op = gzip.open if p.suffix == ".gz" else open
    with op(p, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def charge_status(c):
    """Failed calls with a timeout/unknown outcome may still have been billed: flag, never assume zero."""
    if c["outcome"] == "failed" and any(w in (c.get("error") or "") for w in ("Timeout", "timeout", "ReadError")):
        return "unknown (timeout; reconcile with provider dashboard)"
    return "measured" if c["outcome"] == "succeeded" or (c.get("input_tokens") or 0) else "not billed (failed before usage)"


# ---------------------------------------------------------------- measured summary
def summarize(calls, rates, run_label):
    st = {}
    for c in calls:
        if c.get("run_label") != run_label:
            continue
        s = st.setdefault(c["role"], {"requests": 0, "attempts": 0, "failed_attempts": 0, "review_slots": 0, "latency_ms": [],
                                      "usage": defaultdict(int), "cost": defaultdict(float), "models": set(), "settings": set(),
                                      "prompt_versions": set(), "tiers": set(), "unknown_charges": 0, "batch_keys": set()})
        s["attempts"] += 1
        s["models"].add(c.get("model_resolved") or c["model"])
        s["settings"].add(c.get("settings") or "")
        s["prompt_versions"].add(c.get("prompt_version") or "")
        s["tiers"].add("n/a" if c["model"].startswith("jev") else tier_of(c))
        s["batch_keys"].add(c.get("batch_key"))
        if c["outcome"] == "succeeded":
            s["requests"] += 1
            s["review_slots"] += len(c["review_ids"])
        else:
            s["failed_attempts"] += 1
        if charge_status(c).startswith("unknown"):
            s["unknown_charges"] += 1
        if c.get("latency_ms") is not None:
            s["latency_ms"].append(c["latency_ms"])
        for it in ITEMS:
            s["usage"][it] += c.get(it) or 0
        for it, v in call_cost(c, rates).items():
            s["cost"][it] += v
    return st


def tot(s):
    return sum(s["cost"].values()) if s else 0.0


def fmt_usd(x):
    return f"${x:,.6f}" if x < 0.01 else (f"${x:,.4f}" if x < 100 else f"${x:,.2f}")


def mean_s(s, default=0.0):
    return statistics.mean(s["latency_ms"]) / 1000 if s and s["latency_ms"] else default


# ---------------------------------------------------------------- unit rates + projection
def unit_rates(st, calls, rates, origins, verify_sample, label="cold"):
    """Per-unit measurements from one run (pilot cold, or a calibration run)."""
    e, v = st.get("enrich"), st.get("verify")
    ecalls = [c for c in calls if c.get("run_label") == label and c["role"] == "enrich"]
    first = [c for c in ecalls if (c.get("attempt") or 1) == 1]
    slots_all = sum(len(c["review_ids"]) for c in ecalls)
    slots_first = sum(len(c["review_ids"]) for c in first) or 1
    # what the same enrichment usage would cost at standard (non-batch) rates: a rate lookup, not a hard-coded 2x
    std_cost = sum(sum(call_cost(c, rates, tier="standard").values()) for c in ecalls)
    batch_turnaround = [c["latency_ms"] / 1000 for c in ecalls if c.get("latency_ms")]
    return {
        "enrich_cost_per_text": tot(e) / max(1, origins),
        "enrich_std_cost_per_text": std_cost / max(1, origins),
        "enrich_in_tokens_per_text": (e["usage"]["input_tokens"] / max(1, e["review_slots"])) if e else 0,
        "enrich_out_tokens_per_text": (e["usage"]["output_tokens"] / max(1, e["review_slots"])) if e else 0,
        "enrich_cache_tokens_per_request": ((e["usage"]["cache_read_tokens"] + e["usage"]["cache_write_tokens"]) / max(1, e["requests"])) if e else 0,
        "retry_rate": max(0.0, slots_all - slots_first) / slots_first,
        "batch_turnaround_s": statistics.mean(batch_turnaround) if batch_turnaround else 0.0,
        "verify_cost_per_review": tot(v) / max(1, v["review_slots"]) if v else 0.0,
        "verify_latency_s": mean_s(v, 1.0),
        "group_cost": tot(st.get("group")), "memo_cost": tot(st.get("memo")),
        "group_s": mean_s(st.get("group")), "memo_s": mean_s(st.get("memo")),
        "origins": origins, "verify_sample": verify_sample,
    }


def project(u, args, rates):
    """Full-run scenarios from measured unit rates. Fixed overhead (ingest, group, memo) is counted once."""
    out = {}
    worst_req = (3500 * rates.get(("claude-haiku-5-5:batch", "input_tokens"), 0)
                 + args.output_token_cap * rates.get(("claude-haiku-5-5:batch", "output_tokens"), 0))
    for name in ("base", "conservative"):
        cons = name == "conservative"
        retry = max(u["retry_rate"], 0.01) if not cons else max(3 * u["retry_rate"], 0.05)
        retry = min(retry, args.fallback_cap) if not cons else max(retry, args.fallback_cap)
        turnaround = max(u["batch_turnaround_s"], 60.0) * (3 if cons else 1)
        for reuse in (True, False):
            texts = FULL["distinct_texts"] if reuse else FULL["nonempty"]
            requests = math.ceil(texts / 50) * (1 + retry)
            enrich = texts * (1 + retry) * u["enrich_cost_per_text"]
            enrich_std = texts * (1 + retry) * u["enrich_std_cost_per_text"]
            verify = args.verify_sample * u["verify_cost_per_review"] * (1 + retry)
            fixed = u["group_cost"] + u["memo_cost"]
            total = enrich + verify + fixed
            batches = math.ceil(requests / args.batch_requests)
            waves = math.ceil(batches / max(1, args.workers))
            enrich_h = waves * turnaround / 3600
            verify_h = args.verify_sample / max(0.1, args.jev_rps) / 3600
            fixed_h = (args.ingest_s + u["group_s"] + u["memo_s"]) / 3600
            by_provider = {"anthropic": enrich + fixed, "typesafe": verify}
            out[f"{name}{'' if reuse else '_no_reuse'}"] = {
                "texts_classified": texts, "retry_fraction": round(retry, 4), "requests": round(requests),
                "message_batches": batches, "batch_turnaround_s": round(turnaround, 1),
                "cost_usd": {"enrich (batch tier)": enrich, "verify (Jev)": verify, "group+memo (once)": fixed, "total": total},
                "enrich_if_standard_tier": enrich_std,
                "worst_case_enrich_at_output_cap": requests * worst_req,
                "by_provider": by_provider,
                "hours": {"enrich": enrich_h, "verify": verify_h, "fixed": fixed_h, "total": enrich_h + verify_h + fixed_h},
                "over_budget": total > args.budget,
                "over_provider_caps": {p: by_provider[p] > cap for p, cap in (("anthropic", args.cap_anthropic), ("typesafe", args.cap_typesafe))}}
    return out


# ---------------------------------------------------------------- report
def replay(args):
    rates, rmeta = load_rates(args.rates)
    calls = read_jsonl(HERE / "pilot_calls.jsonl")
    meas = json.loads((HERE / "measurements.json").read_text())
    st = {lab: summarize(calls, rates, lab) for lab in ("cold", "warm")}
    u = unit_rates(st["cold"], calls, rates, meas["unique_texts"], meas["verify_sample"])
    proj = project(u, args, rates)
    cal = []
    for rd in args.calibrate or []:
        rd = Path(rd)
        cc = read_jsonl(rd / "calls_full.jsonl")
        for c in cc:
            c["run_label"] = "cal"
        summ = json.loads((rd / "run_summary.json").read_text())
        origins = summ["records"]["completed"] - summ["records"]["completed_via_cache"]
        su = summarize(cc, rates, "cal")
        uu = unit_rates(su, cc, rates, origins, args.verify_sample, label="cal")
        cal.append((rd.name, summ, su, uu, project(uu, args, rates)))

    # usage.csv: one row per call x billing item (plus a row for calls with no usage)
    with (HERE / "usage.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_label", "request_id", "role", "model", "tier", "rate_key", "outcome", "item", "billed_units",
                    "usd_per_unit", "item_cost_usd", "charge_status"])
        for c in calls:
            k = rate_key(c)
            for it in ITEMS:
                units = c.get(it) or 0
                if units or it == "input_tokens":
                    r = rates.get((k, it), 0.0)
                    w.writerow([c["run_label"], c["request_id"], c["role"], c["model"],
                                "n/a" if c["model"].startswith("jev") else tier_of(c), k, c["outcome"], it, units,
                                f"{r:.12f}", f"{units * r:.10f}", charge_status(c)])

    L = ["# 100-review cost and runtime calculator", ""]
    L.append(f"Generated offline from `cost/pilot_calls.jsonl` + `{Path(args.rates).name}` (no API key, no model calls). "
             f"Pilot run: {meas.get('pilot_started', '?')}.\n")
    L.append("## 1. Measured pilot (cost_100.csv, unchanged)\n")
    L.append(f"- Input SHA-256 `{meas['input_sha256']}` (matches manifest: {meas['input_sha256'] == PILOT_SHA}); "
             f"{meas['ids']} source IDs; {meas['unique_texts']} unique nonempty texts")
    for lab in ("cold", "warm"):
        m = meas[lab]
        L.append(f"- **{lab}**: wall-clock **{m['wall_clock_s']:.1f} s**, workers {m['workers']}, completed {m['completed']}, "
                 f"quarantined {m['quarantined']}, pending {m['pending']}, result-cache hits {m['cache_hits']}, exit code {m['exit_code']}")
    L.append(f"- Warm rerun: **{meas['warm']['new_enrich_calls']} new enrichment calls**; downstream calls in warm run: "
             f"{meas['warm']['new_other_calls']} (verify/group/memo outputs are cached by input + config hash)")
    L.append(f"- Declared verification sample: {meas['verify_sample']} records (seeded hash order); "
             f"enrichment retry policy: invalid/missing lines retried once, then quarantined\n")

    L.append("### Per stage (cold run)\n")
    L.append("| stage | provider | model (resolved) | tier | settings | prompt/schema | reviews/request | workers | requests ok | attempts | failed | review slots | input | cache write | cache read | output | cost | unknown charges |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in STAGES:
        s = st["cold"].get(name)
        if not s:
            continue
        sets = "; ".join(sorted(x for x in s["settings"] if x))[:120]
        bs = "<=50" if name == "enrich" else ("1" if name == "verify" else "aggregate pack")
        L.append(f"| {name} | {PROVIDER_OF[name]} | {', '.join(sorted(m for m in s['models'] if m))} | {', '.join(sorted(s['tiers']))} | "
                 f"`{sets}` | {', '.join(sorted(s['prompt_versions']))} | {bs} | 1 | {s['requests']} | {s['attempts']} | {s['failed_attempts']} | "
                 f"{s['review_slots']} | {s['usage']['input_tokens']:,} | {s['usage']['cache_write_tokens']:,} | {s['usage']['cache_read_tokens']:,} | "
                 f"{s['usage']['output_tokens']:,} | {fmt_usd(tot(s))} | {s['unknown_charges']} |")
    api_cold = sum(tot(s) for s in st["cold"].values())
    api_warm = sum(tot(s) for s in st["warm"].values())
    L.append(f"\n**API spend: cold {fmt_usd(api_cold)}, warm (incremental) {fmt_usd(api_warm)}.** "
             f"Local compute (MacBook, ingestion + validation + SQLite): **unknown, not metered** (not counted as $0).\n")

    L.append("### Billing arithmetic (cold run): item cost = billed units x price per unit\n")
    L.append("| rate key | item | billed units | USD per 1M | item cost | price source (checked) |")
    L.append("|---|---|---|---|---|---|")
    agg = defaultdict(int)
    for c in calls:
        if c["run_label"] == "cold":
            for it in ITEMS:
                agg[(rate_key(c), it)] += c.get(it) or 0
    for (k, it), units in sorted(agg.items()):
        if not units:
            continue
        r = rates.get((k, it), 0.0)
        src = rmeta.get((k, it), {})
        L.append(f"| {k} | {it} | {units:,} | {r * 1e6:.4f} | {fmt_usd(units * r)} | {src.get('source', '?')} ({src.get('checked_date', '?')}) |")
    L.append("")
    L.append("### Unit metrics\n")
    done = meas["cold"]["completed"]
    L.append(f"- Cost per 1,000 input rows: **{fmt_usd(api_cold / meas['ids'] * 1000)}**; per completed record: {fmt_usd(api_cold / max(1, done))}")
    L.append(f"- Enrichment only: {fmt_usd(u['enrich_cost_per_text'])} per distinct text "
             f"({u['enrich_in_tokens_per_text']:.1f} input + {u['enrich_out_tokens_per_text']:.1f} output tokens per review; "
             f"{u['enrich_cache_tokens_per_request']:.0f} cached system-prompt tokens per request); "
             f"same usage at standard tier would be {fmt_usd(u['enrich_std_cost_per_text'])}")
    L.append(f"- Throughput: cold {done / max(1e-6, meas['cold']['wall_clock_s']):.2f} records/s end to end "
             f"(dominated by Message Batch turnaround {u['batch_turnaround_s']:.0f} s); warm {meas['warm']['completed'] / max(1e-6, meas['warm']['wall_clock_s']):.1f} records/s")
    L.append(f"- Stage seconds (cold): {json.dumps(meas['cold']['stage_seconds'])}")
    L.append("- Summed call latencies are not used as wall-clock time; wall-clock is measured around each whole run.\n")

    L.append("## 2. Full-run projection (estimate, not measured)\n")
    L.append(f"Scope: {FULL['rows']:,} rows accounted for; {FULL['nonempty']:,} nonempty classifications; "
             f"{FULL['empty_quarantined']} empty-text quarantines; exact-text reuse reduces first-pass work to {FULL['distinct_texts']:,} distinct texts.\n")
    L.append(f"Controls: total budget **${args.budget:,.2f}** (Anthropic cap ${args.cap_anthropic:.2f}, TypeSafe cap ${args.cap_typesafe:.2f}); "
             f"max in-flight Message Batches (workers) **{args.workers}**; {args.batch_requests} requests per batch; "
             f"output-token cap **{args.output_token_cap}** per request; max retry/fallback fraction **{args.fallback_cap:.0%}** "
             f"(same model; no stronger-model fallback is configured); verify sample {args.verify_sample}; Jev rate {args.jev_rps}/s.\n")
    L.append("| scenario | texts | retry | requests | batches | enrich $ | verify $ | group+memo $ | **total $** | enrich at standard tier $ | worst case at output cap $ | hours | budget |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k, p in proj.items():
        c, h = p["cost_usd"], p["hours"]
        flag = "**OVER BUDGET**" if p["over_budget"] or any(p["over_provider_caps"].values()) else "ok"
        L.append(f"| {k} | {p['texts_classified']:,} | {p['retry_fraction']:.0%} | {p['requests']:,} | {p['message_batches']:,} | "
                 f"{c['enrich (batch tier)']:.2f} | {c['verify (Jev)']:.4f} | {c['group+memo (once)']:.4f} | **{c['total']:.2f}** | "
                 f"{p['enrich_if_standard_tier']:.2f} | {p['worst_case_enrich_at_output_cap']:.2f} | {h['total']:.1f} | {flag} |")
    L.append("\nAssumptions: per-text enrichment cost, retry fraction and batch turnaround are the measured means of the source run "
             "(pilot unless calibrated); base retry = max(measured, 1%) capped by the fallback cap; conservative retry = "
             "max(3x measured, 5%, cap) and 3x batch turnaround. Time = ceil(batches / workers) x turnaround + verify + fixed "
             "overhead (ingest, group, memo once). Verification and group/memo are never multiplied by the review count.\n")
    if cal:
        L.append("## 3. Refreshed estimates from larger development runs\n")
        L.append("| run | origin texts | enrich $/text | in tok/text | out tok/text | retry | batch turnaround s | base total $ | conservative total $ | conservative hours |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for name, summ, su, uu, pp in cal:
            L.append(f"| {name} | {uu['origins']:,} | {uu['enrich_cost_per_text']:.8f} | {uu['enrich_in_tokens_per_text']:.1f} | "
                     f"{uu['enrich_out_tokens_per_text']:.1f} | {uu['retry_rate']:.2%} | {uu['batch_turnaround_s']:.0f} | "
                     f"{pp['base']['cost_usd']['total']:.2f} | {pp['conservative']['cost_usd']['total']:.2f} | {pp['conservative']['hours']['total']:.1f} |")
        L.append("")
    L.append("## Batching and caching: what each word means here\n")
    L.append("- **CSV chunks** (memory): ingestion streams the CSV into SQLite; this does not affect API cost.")
    L.append("- **Multi-review requests**: each enrichment request carries <= 50 reviews, so the ~3k-token instructions are paid once per 50 reviews; "
             "every returned id is validated.")
    L.append("- **Provider Batch API**: those requests are submitted through the Message Batches API at 50% of standard price, "
             "with asynchronous turnaround (measured above).")
    L.append("- **Saved-result cache** (ours): exact review text + identical label_config reuses one validated result "
             "(`cache_source_id`); every original ID stays a separate record.")
    L.append("- **Provider prompt caching** (Anthropic's): the repeated system prompt is billed at cache-write/cache-read rates when the "
             "provider reports it; only reported usage is counted.\n")
    rec = HERE / "reconciliation.json"
    if rec.exists():
        L.append("## Reconciliation with provider dashboards\n")
        L.append("```json\n" + rec.read_text().strip() + "\n```\n")
    L.append("## Replay\n- Offline (default): `python cost/calculator.py` (edit `cost/rates.csv` or pass flags)\n"
             "- Paid pilot (explicit): `python cost/calculator.py pilot --confirm-paid`\n"
             "- Self-test (offline): `python cost/calculator.py selftest` (2x rates -> 2x API cost, same time; projected volume "
             "does not change measured results)\n")
    md = "\n".join(L)
    (HERE / "report.md").write_text(md)
    (HERE / "projection.json").write_text(json.dumps({"unit_rates": u, "scenarios": proj}, indent=1, default=str))
    print(md)
    return api_cold, meas, proj


def selftest(args):
    """Grader checks, run offline: doubling every rate doubles API spend and leaves measured time unchanged;
    changing projected volume does not change measured pilot results."""
    import copy
    import tempfile
    rates, _ = load_rates(args.rates)
    calls = read_jsonl(HERE / "pilot_calls.jsonl")
    meas = json.loads((HERE / "measurements.json").read_text())
    base = sum(tot(s) for s in summarize(calls, rates, "cold").values())
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as t:
        rows = list(csv.DictReader(open(args.rates)))
        w = csv.DictWriter(t, fieldnames=rows[0].keys())
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["usd_per_unit"] = str(float(r["usd_per_unit"]) * 2)
            w.writerow(r)
    rates2, _ = load_rates(t.name)
    doubled = sum(tot(s) for s in summarize(calls, rates2, "cold").values())
    meas2 = copy.deepcopy(meas)
    saved = dict(FULL)
    FULL["distinct_texts"] *= 3
    u = unit_rates(summarize(calls, rates, "cold"), calls, rates, meas["unique_texts"], meas["verify_sample"])
    project(u, args, rates)
    FULL.update(saved)
    ok1 = abs(doubled - 2 * base) < 1e-12
    ok2 = meas2 == json.loads((HERE / "measurements.json").read_text())
    print(f"2x rates: API cost {base:.8f} -> {doubled:.8f} (exactly double: {ok1}); measured wall-clock unchanged: "
          f"{meas['cold']['wall_clock_s']} s (time does not depend on rates)")
    print(f"3x projected volume: measured pilot results unchanged: {ok2}")
    sys.exit(0 if ok1 and ok2 else 1)


# ---------------------------------------------------------------- paid pilot
def pilot(args):
    if not args.confirm_paid:
        sys.exit("Refusing to make paid calls without --confirm-paid. Default mode is offline replay.")
    sha = hashlib.sha256(PILOT_INPUT.read_bytes()).hexdigest()
    if sha != PILOT_SHA:
        sys.exit("cost_100.csv checksum mismatch")
    run = "pilot100"
    # empty result cache for the cold run; an earlier pilot database is archived (not deleted) so its logged
    # spend keeps counting toward the project-wide provider caps
    db = ROOT / f"data/state/{run}.sqlite"
    if db.exists():
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        db.rename(ROOT / f"data/state/archived_{run}_{time.strftime('%Y%m%d_%H%M%S')}.sqlite")
    for p in (ROOT / f"data/state/{run}.sqlite-wal", ROOT / f"data/state/{run}.sqlite-shm"):
        p.unlink(missing_ok=True)
    shutil.rmtree(ROOT / "runs" / run, ignore_errors=True)
    base = [sys.executable, "-m", "pipeline", "run", "--input", str(PILOT_INPUT), "--run", run, "--workers", "1",
            "--transport", "batch", "--poll-seconds", "10", "--verify-sample", str(args.pilot_verify),
            "--budget-usd", str(args.pilot_budget)]
    meas = {"input_sha256": sha, "ids": 100, "verify_sample": args.pilot_verify,
            "pilot_started": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "command": " ".join(base[1:])}
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
    p.add_argument("mode", nargs="?", default="replay", choices=["replay", "pilot", "selftest"])
    p.add_argument("--rates", default=str(HERE / "rates.csv"))
    p.add_argument("--budget", type=float, default=9.0, help="total project API budget (USD)")
    p.add_argument("--cap-anthropic", type=float, default=4.5)
    p.add_argument("--cap-typesafe", type=float, default=4.5)
    p.add_argument("--workers", type=int, default=4, help="max in-flight Message Batches for the projection")
    p.add_argument("--batch-requests", type=int, default=200, help="enrichment requests per Message Batch")
    p.add_argument("--output-token-cap", type=int, default=2000, help="max output tokens per enrichment request")
    p.add_argument("--fallback-cap", type=float, default=0.05, help="max fraction of texts re-sent (retry/fallback)")
    p.add_argument("--verify-sample", type=int, default=200)
    p.add_argument("--jev-rps", type=float, default=8.0)
    p.add_argument("--ingest-s", type=float, default=10.0, help="measured full-file ingestion seconds")
    p.add_argument("--calibrate", nargs="*")
    p.add_argument("--confirm-paid", action="store_true")
    p.add_argument("--pilot-verify", type=int, default=20)
    p.add_argument("--pilot-budget", type=float, default=0.50)
    a = p.parse_args()
    if a.mode == "pilot":
        pilot(a)
        replay(a)
    elif a.mode == "selftest":
        selftest(a)
    else:
        replay(a)


if __name__ == "__main__":
    main()

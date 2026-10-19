"""Stage 2 - Classify (role: enrich).

Code chooses the work: pending nonempty reviews are deduplicated by exact text, ordered by how many
pending reviews share each text (so an early stop still covers the most rows), and split into requests
of <= 50 distinct texts. Claude Haiku 5.5 (effort low, thinking disabled) labels each request with one
coded line per review (prompts/enrich_codes_v1.md). Code then:

  * validates every line: known local id, no duplicates, allowed codes, subtopic belongs to the topic;
  * applies the shared-definition consistency rules (logged in `detail`, never silent);
  * extracts the evidence quote and entities deterministically (always exact substrings);
  * commits each request atomically together with its call-log row; every other review with identical
    text is completed in the same transaction with cache_source_id pointing at the origin review.

Reviews missing from a response, or on a malformed line, get one retry round; a second failure
quarantines them with the reason. Rate limits and server errors use bounded backoff.

Two transports send the identical request:
  batch (default)  Message Batches API at 50% price. Batches are recorded in the `batches` table before
                   their results arrive; an interrupted run leaves them there and the next invocation
                   collects their results instead of resubmitting (no double charge, no relabeling).
  sync             Messages API, `workers` concurrent requests. Used for quick checks and fresh-input demos.
"""
import asyncio
import json
import re
import time

from . import extract, settings
from .providers import BudgetStop, CallFailed, cost_usd, usage_of

SYSTEM = (settings.PROMPTS / "enrich_codes_v1.md").read_text()
TOPIC_CODES = {"A": "access", "U": "usability", "P": "playback", "D": "downloads", "C": "catalog", "B": "billing",
               "S": "support", "O": "other"}
INTENT_CODES = {"X": "cancellation", "C": "complaint", "R": "request", "P": "praise", "U": "unclear"}
LINE = re.compile(r"^(r\d+)\s+([AUPDCBSO])\s+([a-z_]+)\s+([XCRPU])\s+([1-5])\s+(-2|-1|0|1|2)\s+([01])\s*$")
ID_ONLY = re.compile(r"^(r\d+)\b")


def consistency(labels, adjustments):
    """Shared-definition rules enforced by code (logged, never silent)."""
    if labels["intent"] in ("praise", "request", "unclear") and labels["severity"] != 1:
        adjustments.append(f"severity {labels['severity']}->1 (intent {labels['intent']} reports no problem)")
        labels["severity"] = 1
    if labels["intent"] == "complaint" and labels["severity"] == 1:
        adjustments.append("severity 1->2 (a complaint is at least generic criticism)")
        labels["severity"] = 2
    if not labels["subtopic"] or not labels["subtopic"].startswith(labels["topic"] + "."):
        new = labels["topic"] + (".other" if labels["topic"] + ".other" in settings.SUBTOPICS else "")
        if labels["topic"] == "other":
            new = "other.general_positive" if labels["intent"] == "praise" else (
                "other.unrelated_unclear" if labels["intent"] == "unclear" else "other.general_negative")
        if labels["topic"] == "support":
            new = "support.unhelpful"
        adjustments.append(f"subtopic {labels['subtopic']}->{new} (must be a listed subtopic of the topic)")
        labels["subtopic"] = new
    labels["sentiment"] = max(-1.0, min(1.0, round(float(labels["sentiment"]), 2)))
    return labels


def build_request(items):
    """items: list of (review_id, text), <= 50. Returns ({local_id: review_id}, Messages API params)."""
    assert 0 < len(items) <= 50, "enrichment requests carry at most 50 reviews"
    local = {f"r{i + 1}": rid for i, (rid, _) in enumerate(items)}
    user = "Classify each review. One line per id.\n<reviews>\n" + json.dumps(
        [{"id": f"r{i + 1}", "text": t} for i, (_, t) in enumerate(items)], ensure_ascii=False) + "\n</reviews>"
    params = {"model": settings.HAIKU, "max_tokens": settings.ENRICH_MAX_TOKENS,
              "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
              "messages": [{"role": "user", "content": user}],
              "output_config": {"effort": "low"}, "thinking": {"type": "disabled"}}
    return local, params


def parse(text, local, texts):
    """Validate one response. Returns ({review_id: labels}, {review_id: problem})."""
    labels, problems, seen = {}, {}, set()
    for raw in text.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        m = LINE.match(line)
        if not m:
            idm = ID_ONLY.match(line)
            if idm and idm.group(1) in local and local[idm.group(1)] not in labels:
                problems[local[idm.group(1)]] = f"malformed_line: {line[:80]}"
            continue
        lid, tc, sub, ic, sev, sen, nr = m.groups()
        rid = local.get(lid)
        if rid is None:
            continue                     # unknown id: ignored, never mapped to a review
        if lid in seen:
            problems[rid] = "duplicate_id_in_output"
            labels.pop(rid, None)
            continue
        seen.add(lid)
        topic = TOPIC_CODES[tc]
        lab = {"topic": topic, "subtopic": f"{topic}.{sub}" if f"{topic}.{sub}" in settings.SUBTOPICS else None,
               "intent": INTENT_CODES[ic], "severity": int(sev), "sentiment": int(sen) / 2, "needs_review": nr == "1"}
        adjustments = []
        consistency(lab, adjustments)
        t = texts[rid]
        lab["evidence_quote"] = extract.evidence_quote(t, lab["topic"], lab["intent"])
        lab["entities"] = extract.entities(t)
        lab["detail"] = {"raw_line": line, "adjustments": adjustments, "quote_source": "deterministic_extract"}
        if extract.non_latin_share(t) > 0.5 and not lab["needs_review"]:
            lab["needs_review"] = True
            lab["detail"]["needs_review_reason"] = "non_latin_script"
        labels[rid] = lab
        problems.pop(rid, None)
    for rid in local.values():
        if rid not in labels and rid not in problems:
            problems[rid] = "missing_from_output"
    return labels, problems


def _copy_to_duplicate(db, origin, rid, invocation):
    db.execute("""UPDATE records SET (status, reason, label_config, topic, subtopic, intent, sentiment, severity, entities,
                  evidence_quote, needs_review, label_source, detail, cache_source_id, invocation, completed_at) =
                  (SELECT 'completed', NULL, label_config, topic, subtopic, intent, sentiment, severity, entities, evidence_quote,
                  needs_review, label_source, '{"cache":"exact_text_reuse"}', ?, ?, ? FROM records WHERE review_id = ?)
                  WHERE review_id = ? AND status = 'pending'""", (origin, invocation, time.time(), origin, rid))


def _resolve_cache(store, invocation):
    """Complete pending reviews whose exact text already has a completed origin (same label_config)."""
    db = store.db
    rows = db.execute("""
        SELECT p.review_id, o.review_id AS origin FROM reviews pr JOIN records p ON p.review_id = pr.review_id
        JOIN reviews orv ON orv.text_sha = pr.text_sha JOIN records o ON o.review_id = orv.review_id
        WHERE p.status = 'pending' AND o.status = 'completed' AND o.cache_source_id IS NULL AND o.label_config = ?
        GROUP BY p.review_id""", (settings.LABEL_CONFIG,)).fetchall()
    if rows:
        db.execute("BEGIN")
        for r in rows:
            _copy_to_duplicate(db, r["origin"], r["review_id"], invocation)
        db.execute("COMMIT")
    return len(rows)


def commit_request(db, origins, labels, problems, invocation, source, call_row=None, log_call=None):
    """One atomic transaction: the call-log row (if any), completed origins + their exact duplicates,
    and attempt accounting for failed reviews. Only rows still pending are touched (idempotent)."""
    now = time.time()
    db.execute("BEGIN")
    try:
        if call_row is not None:
            log_call(**call_row)
        for o in origins:
            rid = o["review_id"]
            st = db.execute("SELECT status, attempts FROM records WHERE review_id=?", (rid,)).fetchone()
            if st["status"] != "pending":
                continue
            if rid in labels:
                lab = labels[rid]
                db.execute("""UPDATE records SET status='completed', reason=NULL, attempts=attempts+1, label_config=?, topic=?,
                              subtopic=?, intent=?, sentiment=?, severity=?, entities=?, evidence_quote=?, needs_review=?,
                              cache_source_id=NULL, label_source=?, detail=?, invocation=?, completed_at=? WHERE review_id=?""",
                           (settings.LABEL_CONFIG, lab["topic"], lab["subtopic"], lab["intent"], lab["sentiment"], lab["severity"],
                            json.dumps(lab["entities"], ensure_ascii=False), lab["evidence_quote"], int(lab["needs_review"]),
                            source, json.dumps(lab["detail"], ensure_ascii=False), invocation, now, rid))
                for d in db.execute("""SELECT r.review_id FROM reviews r JOIN records c ON c.review_id=r.review_id
                                       WHERE r.text_sha=? AND c.status='pending' AND r.review_id<>?""", (o["text_sha"], rid)).fetchall():
                    _copy_to_duplicate(db, rid, d[0], invocation)
            else:
                att = st["attempts"] + 1
                why = problems.get(rid, "no_result")
                if att >= 2:      # first try + one retry, then quarantine with the reason
                    db.execute("UPDATE records SET status='quarantined', reason=?, attempts=? WHERE review_id=?",
                               (f"enrichment_failed: {why}"[:300], att, rid))
                else:
                    db.execute("UPDATE records SET attempts=?, reason=? WHERE review_id=?", (att, f"retry_pending: {why}"[:300], rid))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return sum(1 for o in origins if o["review_id"] in labels)


def _pending_origins(store, only_ids=None):
    """One origin per distinct pending text, most-duplicated texts first, excluding reviews already
    carried by an open (submitted, not yet collected) Message Batch."""
    db = store.db
    in_flight = set()
    for (reqs,) in db.execute("SELECT requests FROM batches WHERE status='submitted'"):
        for ids in json.loads(reqs).values():
            in_flight.update(ids)
    rows = db.execute("""
        WITH p AS (SELECT r.text_sha, COUNT(*) n, MIN(r.row_idx) idx FROM reviews r JOIN records c ON c.review_id=r.review_id
                   WHERE c.status='pending' GROUP BY r.text_sha)
        SELECT r.review_id, r.review_text, r.text_sha, p.n FROM p JOIN reviews r ON r.row_idx = p.idx
        ORDER BY p.n DESC, p.idx""").fetchall()
    rows = [r for r in rows if r["review_id"] not in in_flight]
    if only_ids is not None:
        rows = [r for r in rows if r["review_id"] in only_ids]
    return rows


def _worst_case(prov, n_requests, tier):
    """Reservation per request: generous input estimate + full output cap."""
    per = cost_usd(prov.rates, settings.HAIKU, {"input_tokens": 3500, "cache_write_tokens": 2600,
                                                 "output_tokens": settings.ENRICH_MAX_TOKENS}, tier)
    return per * n_requests


async def run(store, prov, opts, invocation, log, only_ids=None, retry_round=0):
    db = store.db
    reused = _resolve_cache(store, invocation)
    if reused:
        log(f"enrich: {reused} pending reviews completed from exact-text cache (no model call)")
    state = {"done": 0, "failed": 0, "stop": None, "requests": 0}
    if opts["transport"] == "batch":
        await _collect_open_batches(store, prov, opts, invocation, log, state)
        if state["stop"]:
            return state
    origins = _pending_origins(store, only_ids)
    B = opts["batch_size"]
    chunks = [origins[i:i + B] for i in range(0, len(origins), B)]
    log(f"enrich: {len(origins)} distinct pending texts -> {len(chunks)} requests (<= {B} reviews each); "
        f"transport={opts['transport']} workers={opts['workers']} retry_round={retry_round}; label_config={settings.LABEL_CONFIG}")
    if chunks:
        if opts["transport"] == "batch":
            await _run_batches(store, prov, opts, invocation, log, chunks, state, retry_round)
        else:
            await _run_sync(store, prov, opts, invocation, log, chunks, state)
    if not state["stop"] and retry_round == 0:
        retry = {r[0] for r in db.execute("SELECT review_id FROM records WHERE status='pending' AND attempts>=1")}
        if retry:
            log(f"enrich: retry round for {len(retry)} reviews that failed once")
            st2 = await run(store, prov, opts, invocation, log, only_ids=retry, retry_round=1)
            for k in ("done", "failed", "requests"):
                state[k] += st2[k]
            state["stop"] = st2["stop"]
    return state


def _stopping(prov):
    return prov.ledger.stop_reason == "interrupted"


# ------------------------------------------------------------------ sync transport
async def _run_sync(store, prov, opts, invocation, log, chunks, state):
    queue = asyncio.Queue()
    for k, c in enumerate(chunks):
        queue.put_nowait((k, c))
    t0 = time.time()

    async def worker():
        while not queue.empty() and not state["stop"]:
            k, chunk = queue.get_nowait()
            items = [(o["review_id"], o["review_text"]) for o in chunk]
            local, params = build_request(items)
            texts = dict(items)
            try:
                text, usage, stop_reason = await prov.claude_text(
                    params, role="enrich", review_ids=[r for r, _ in items], prompt_version=settings.ENRICH_PROMPT_VERSION,
                    batch_key=f"inv{invocation}-s{k}", label_config=settings.LABEL_CONFIG)
                labels, problems = parse(text, local, texts)
                if stop_reason == "max_tokens":
                    for rid in problems:
                        problems[rid] += " (stop_reason=max_tokens)"
            except BudgetStop as e:
                state["stop"] = prov.ledger.stop_reason or "spend_cap"
                log(f"enrich: STOP admitting work - {e}")
                return
            except CallFailed as e:
                labels, problems = {}, {rid: f"call_failed: {str(e)[:150]}" for rid, _ in items}
            n = commit_request(store.db, chunk, labels, problems, invocation, "haiku")
            state["done"] += n
            state["failed"] += len(chunk) - n
            state["requests"] += 1
            if k % 10 == 0 or queue.empty():
                log(f"enrich: request {k + 1}/{len(chunks)} | texts done {state['done']} failed {state['failed']} | "
                    f"spent ${prov.ledger.spent:.4f} | {state['done'] / max(time.time() - t0, 1e-6):.1f} texts/s")
            if _stopping(prov):
                state["stop"] = "interrupted"
                return

    await asyncio.gather(*[worker() for _ in range(max(1, opts["workers"]))])


# ------------------------------------------------------------------ batch transport
async def _run_batches(store, prov, opts, invocation, log, chunks, state, retry_round):
    db = store.db
    per_batch = max(1, opts["batch_requests"])
    groups = [chunks[i:i + per_batch] for i in range(0, len(chunks), per_batch)]
    inflight = {}   # batch_id -> (reservation, {custom_id: chunk})
    gi = 0
    t0 = time.time()
    while (gi < len(groups) or inflight) and not state["stop"]:
        # admit new batches up to the worker limit
        while gi < len(groups) and len(inflight) < max(1, opts["workers"]) and not _stopping(prov):
            group = groups[gi]
            try:
                reservation = prov.ledger.admit(_worst_case(prov, len(group), "batch"))
            except BudgetStop as e:
                log(f"enrich: STOP admitting new batches - {e}")
                gi = len(groups)
                if not inflight:
                    state["stop"] = prov.ledger.stop_reason or "spend_cap"
                break
            requests, mapping = [], {}
            for j, chunk in enumerate(group):
                cid = f"inv{invocation}-g{gi}-q{j}"
                _, params = build_request([(o["review_id"], o["review_text"]) for o in chunk])
                requests.append({"custom_id": cid, "params": params})
                mapping[cid] = [o["review_id"] for o in chunk]
            try:
                batch = await prov.batch_submit(requests)
            except CallFailed as e:
                prov.ledger.settle(reservation, 0)
                log(f"enrich: batch submit failed ({e}); stopping - pending work stays pending")
                state["stop"] = "submit_failed"
                break
            db.execute("INSERT INTO batches(batch_id, invocation, submitted_at, status, request_count, requests, reserved_usd, "
                       "label_config, retry_round) VALUES(?,?,?,?,?,?,?,?,?)",
                       (batch.id, invocation, time.time(), "submitted", len(requests), json.dumps(mapping), reservation,
                        settings.LABEL_CONFIG, retry_round))
            inflight[batch.id] = reservation
            log(f"enrich: submitted batch {gi + 1}/{len(groups)} {batch.id} ({len(requests)} requests, "
                f"{sum(len(v) for v in mapping.values())} reviews, reserved ${reservation:.4f})")
            gi += 1
        if not inflight:
            break
        # wait, interruptibly
        for _ in range(int(opts["poll_seconds"])):
            if _stopping(prov):
                break
            await asyncio.sleep(1)
        if _stopping(prov):
            log(f"enrich: interrupted - {len(inflight)} submitted batch(es) left open; a resume collects them (no resubmission)")
            for bid, res in inflight.items():
                prov.ledger.settle(res, 0)
            state["stop"] = "interrupted"
            return
        for bid in list(inflight):
            b = await prov.batch_retrieve(bid)
            if b.processing_status == "ended":
                await _ingest_batch(store, prov, invocation, bid, log, state)
                prov.ledger.settle(inflight.pop(bid), 0)  # actual cost was added to the ledger per request
                el = time.time() - t0
                log(f"enrich: collected {bid} | texts done {state['done']} failed {state['failed']} | "
                    f"spent ${prov.ledger.spent:.4f} | {state['done'] / max(el, 1e-6):.1f} texts/s overall")
            else:
                rc = b.request_counts
                log(f"enrich: {bid} {b.processing_status}: processing={rc.processing} succeeded={rc.succeeded} errored={rc.errored}")


async def _collect_open_batches(store, prov, opts, invocation, log, state):
    """Resume path: batches submitted by an earlier (interrupted) invocation are collected, not resubmitted."""
    db = store.db
    open_ = db.execute("SELECT batch_id, request_count FROM batches WHERE status='submitted' ORDER BY submitted_at").fetchall()
    if not open_:
        return
    log(f"enrich: {len(open_)} open batch(es) from an earlier invocation - collecting their results first")
    reservation = prov.ledger.admit(_worst_case(prov, sum(r["request_count"] for r in open_), "batch")) if open_ else 0
    pending = [r["batch_id"] for r in open_]
    while pending:
        for bid in list(pending):
            b = await prov.batch_retrieve(bid)
            if b.processing_status == "ended":
                await _ingest_batch(store, prov, invocation, bid, log, state)
                pending.remove(bid)
        if pending:
            for _ in range(int(opts["poll_seconds"])):
                if _stopping(prov):
                    break
                await asyncio.sleep(1)
            if _stopping(prov):
                state["stop"] = "interrupted"
                break
    prov.ledger.settle(reservation, 0)


async def _ingest_batch(store, prov, invocation, batch_id, log, state):
    db = store.db
    row = db.execute("""SELECT b.submitted_at, b.requests, b.retry_round, COALESCE(i.phase, 'initial') AS phase
                        FROM batches b LEFT JOIN invocations i ON i.invocation = b.invocation WHERE b.batch_id=?""",
                     (batch_id,)).fetchone()
    mapping = json.loads(row["requests"])
    already = {r[0] for r in db.execute("SELECT request_id FROM calls WHERE request_id LIKE ?", (batch_id + "/%",))}
    entries = await prov.batch_results(batch_id)
    seen = set()
    now = time.time()
    for e in entries:
        cid = e.custom_id
        seen.add(cid)
        rid_list = mapping.get(cid)
        req_id = f"{batch_id}/{cid}"
        if rid_list is None or req_id in already:
            continue        # foreign entry, or already ingested before a crash
        origins = db.execute(f"SELECT review_id, review_text, text_sha FROM reviews WHERE review_id IN ({','.join('?' * len(rid_list))})",
                             rid_list).fetchall()
        order = {rid: i for i, rid in enumerate(rid_list)}
        origins = sorted(origins, key=lambda o: order[o["review_id"]])
        local = {f"r{i + 1}": rid for i, rid in enumerate(rid_list)}
        texts = {o["review_id"]: o["review_text"] for o in origins}
        call = dict(request_id=req_id, role="enrich", phase=row["phase"], invocation=invocation, provider="anthropic",
                    model=settings.HAIKU, settings=json.dumps({"tier": "batch", "effort": "low", "thinking": "disabled",
                                                               "max_tokens": settings.ENRICH_MAX_TOKENS}, sort_keys=True),
                    prompt_version=settings.ENRICH_PROMPT_VERSION, label_config=settings.LABEL_CONFIG, review_ids=rid_list,
                    attempt=1 + row["retry_round"], started_at=row["submitted_at"],
                    latency_ms=int((now - row["submitted_at"]) * 1000), batch_key=batch_id)
        if e.result.type == "succeeded":
            msg = e.result.message
            usage = usage_of(msg.usage)
            c = cost_usd(prov.rates, settings.HAIKU, usage, "batch")
            text = next((b.text for b in msg.content if b.type == "text"), "")
            if prov.faults.take("enrich", "badjson"):
                text = text[: len(text) // 2] + "\n#garbled"
            labels, problems = parse(text, local, texts)
            if msg.stop_reason == "max_tokens":
                for rid in problems:
                    problems[rid] += " (stop_reason=max_tokens)"
            call.update(model_resolved=msg.model, outcome="succeeded" if labels else "failed", cost_usd=c, **usage)
            if problems:
                call["error"] = f"invalid_output for {len(problems)}/{len(rid_list)} reviews: " + "; ".join(sorted(set(problems.values())))[:200]
            prov.ledger.spent += c
        else:
            err = getattr(getattr(e.result, "error", None), "error", None) or getattr(e.result, "error", None)
            labels, problems = {}, {rid: f"batch_{e.result.type}" for rid in rid_list}
            call.update(outcome="failed", error=f"batch result {e.result.type}: {str(err)[:200]}", input_tokens=0,
                        output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.0)
        call["review_ids"] = json.dumps(call["review_ids"])
        n = commit_request(db, origins, labels, problems, invocation, "haiku", call_row=call, log_call=_insert_call(db))
        state["done"] += n
        state["failed"] += len(origins) - n
        state["requests"] += 1
    for cid in set(mapping) - seen:   # requests the provider never returned: count as failed attempts
        rid_list = mapping[cid]
        origins = db.execute(f"SELECT review_id, review_text, text_sha FROM reviews WHERE review_id IN ({','.join('?' * len(rid_list))})",
                             rid_list).fetchall()
        commit_request(db, origins, {}, {rid: "batch_result_missing" for rid in rid_list}, invocation, "haiku")
    db.execute("UPDATE batches SET status='ended', ended_at=? WHERE batch_id=?", (time.time(), batch_id))


def _insert_call(db):
    def ins(**c):
        cols = ",".join(c)
        db.execute(f"INSERT INTO calls({cols}) VALUES({','.join('?' * len(c))})", tuple(c.values()))
    return ins

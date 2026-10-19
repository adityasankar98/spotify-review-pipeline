"""Stage 2 - Classify (role: enrich).

Code chooses the work: pending nonempty reviews are deduplicated by exact text, split into batches
of <= 50 distinct texts, and dispatched by a bounded worker pool. For each text:

  1. Jev (TypeSafe) answers fixed Choice/Score/Noul questions about the review text alone.
  2. Code maps probabilities into the common schema, applies the shared-definition consistency
     rules, extracts an exact evidence quote and explicit entities, and routes low-confidence or
     unsupported-language texts to the fallback.
  3. Claude Haiku 5.5 (fallback, <= 50 reviews per request, capped fraction) re-labels routed texts
     from the original text and definitions; its quote/entities are validated in code.

Results are committed in one SQLite transaction per batch. Every other review with identical text
is completed in the same transaction with cache_source_id pointing at the origin review.
"""
import asyncio
import json
import time

from . import extract, settings
from .providers import BudgetStop, CallFailed

SUB_KEYS = list(settings.SUBTOPICS)
FALLBACK_SYSTEM = (settings.PROMPTS / "enrich_fallback_v1.md").read_text()
FALLBACK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "topic", "subtopic", "intent", "severity", "sentiment", "quote", "entities", "needs_review"],
        "properties": {
            "id": {"type": "string"}, "topic": {"type": "string", "enum": list(settings.TOPICS)},
            "subtopic": {"type": "string", "enum": SUB_KEYS}, "intent": {"type": "string", "enum": list(settings.INTENTS)},
            "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]}, "sentiment": {"type": "number"},
            "quote": {"type": "string"}, "entities": {"type": "array", "items": {"type": "string"}},
            "needs_review": {"type": "boolean"}}}}}}


def consistency(labels, adjustments):
    """Shared-definition rules enforced by code (logged, never silent)."""
    if labels["intent"] in ("praise", "request", "unclear") and labels["severity"] != 1:
        adjustments.append(f"severity {labels['severity']}->1 (intent {labels['intent']} reports no problem)")
        labels["severity"] = 1
    if labels["intent"] == "complaint" and labels["severity"] == 1:
        adjustments.append("severity 1->2 (a complaint is at least generic criticism)")
        labels["severity"] = 2
    if not labels["subtopic"].startswith(labels["topic"] + "."):
        new = labels["topic"] + (".other" if labels["topic"] + ".other" in settings.SUBTOPICS else "")
        if labels["topic"] == "other":
            new = "other.general_positive" if labels["intent"] == "praise" else (
                "other.unrelated_unclear" if labels["intent"] == "unclear" else "other.general_negative")
        if labels["topic"] == "support":
            new = "support.unhelpful"
        adjustments.append(f"subtopic {labels['subtopic']}->{new} (parent must equal topic)")
        labels["subtopic"] = new
    labels["sentiment"] = max(-1.0, min(1.0, round(float(labels["sentiment"]), 2)))
    return labels


def from_jev(text, j):
    topic = j["topic"]["choice"]
    labels = {"topic": topic, "intent": j["intent"]["choice"], "severity": int(j["severity"]["choice"]),
              "sentiment": j["sentiment"]["score"] / 2 - 1}
    labels["subtopic"] = extract.subtopic(text, topic, labels["intent"])
    reasons = []
    if j["topic"]["confidence"] < settings.FALLBACK_TOPIC_CONF:
        reasons.append(f"topic_conf={j['topic']['confidence']:.2f}")
    if j["intent"]["confidence"] < settings.FALLBACK_INTENT_CONF:
        reasons.append(f"intent_conf={j['intent']['confidence']:.2f}")
    if extract.non_latin_share(text) > 0.5:
        reasons.append("non_latin_script")
    adjustments = []
    consistency(labels, adjustments)
    detail = {"jev_model": j["model"], "conf": {k: round(j[k]["confidence"], 3) for k in ("topic", "intent", "severity")},
              "sentiment_score": round(j["sentiment"]["score"], 3),
              "topic_probs": {k: round(v, 3) for k, v in sorted(j["topic"]["probabilities"].items(), key=lambda x: -x[1])[:3]},
              "severity_probs": {k: round(v, 3) for k, v in j["severity"]["probabilities"].items()},
              "route_reasons": reasons, "adjustments": adjustments}
    labels["needs_review"] = bool(reasons)
    labels["evidence_quote"] = extract.evidence_quote(text, labels["topic"], labels["intent"])
    labels["entities"] = extract.entities(text)
    return labels, detail


def apply_fallback(text, labels, detail, f):
    new = {"topic": f["topic"], "subtopic": f["subtopic"], "intent": f["intent"], "severity": int(f["severity"]),
           "sentiment": f["sentiment"]}
    adjustments = []
    consistency(new, adjustments)
    quote = f["quote"].strip()
    if quote and quote in text:
        new["evidence_quote"], qsrc = quote, "fallback_model"
    else:
        new["evidence_quote"], qsrc = extract.evidence_quote(text, new["topic"], new["intent"]), "deterministic_repair"
    low = text.lower()
    ents = [e for e in f["entities"] if e.strip() and e.lower() in low]
    new["entities"] = ents
    new["needs_review"] = bool(f["needs_review"])
    detail.update({"fallback": {"jev_labels": {k: labels[k] for k in ("topic", "subtopic", "intent", "severity")},
                                "adjustments": adjustments, "quote_source": qsrc,
                                "dropped_entities": [e for e in f["entities"] if e not in ents]}})
    return new


async def _fallback(prov, opts, items, batch_key):
    """items: list of (review_id, text). Returns {review_id: fallback dict}."""
    local = {f"r{i + 1}": rid for i, (rid, _) in enumerate(items)}
    user = "Classify each review. Return one result per id.\n<reviews>\n" + json.dumps(
        [{"id": f"r{i + 1}", "text": t} for i, (_, t) in enumerate(items)], ensure_ascii=False) + "\n</reviews>"

    def validate(data):
        ids = [r["id"] for r in data.get("results", [])]
        if sorted(ids) != sorted(local) or len(set(ids)) != len(ids):
            return f"returned ids {sorted(set(ids) ^ set(local))[:5]} mismatch expected {len(local)}"
        return None

    data, _ = await prov.claude_json(role="enrich", model=settings.HAIKU, system=FALLBACK_SYSTEM, user=user,
                                     schema=FALLBACK_SCHEMA, max_tokens=opts["fallback_max_tokens"], effort="low",
                                     review_ids=[rid for rid, _ in items], prompt_version=settings.FALLBACK_PROMPT_VERSION,
                                     batch_key=batch_key, label_config=settings.LABEL_CONFIG, validate=validate)
    return {local[r["id"]]: r for r in data["results"]}


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


def _copy_to_duplicate(db, origin, rid, invocation):
    db.execute("""UPDATE records SET (status, reason, label_config, topic, subtopic, intent, sentiment, severity, entities,
                  evidence_quote, needs_review, label_source, detail, cache_source_id, invocation, completed_at) =
                  (SELECT 'completed', NULL, label_config, topic, subtopic, intent, sentiment, severity, entities, evidence_quote,
                  needs_review, label_source, '{"cache":"exact_text_reuse"}', ?, ?, ? FROM records WHERE review_id = ?)
                  WHERE review_id = ? AND status = 'pending'""", (origin, invocation, time.time(), origin, rid))


async def run(store, prov, opts, invocation, log, only_ids=None):
    db = store.db
    reused = _resolve_cache(store, invocation)
    if reused:
        log(f"enrich: {reused} pending reviews completed from exact-text cache (no model call)")
    q = """SELECT r.text_sha, MIN(r.row_idx) AS idx FROM reviews r JOIN records c ON c.review_id = r.review_id
           WHERE c.status = 'pending' GROUP BY r.text_sha ORDER BY idx"""
    origins = db.execute(f"SELECT r.review_id, r.review_text, r.text_sha FROM reviews r JOIN ({q}) m ON r.row_idx = m.idx "
                         "ORDER BY m.idx").fetchall()
    if only_ids is not None:
        origins = [o for o in origins if o["review_id"] in only_ids]
    total_texts = db.execute("SELECT COUNT(DISTINCT text_sha) FROM reviews WHERE is_empty=0").fetchone()[0]
    fb_used = db.execute("SELECT COUNT(*) FROM records WHERE label_source='jev+haiku_fallback' AND cache_source_id IS NULL").fetchone()[0]
    fb_cap = int(opts["max_fallback_fraction"] * total_texts)
    state = {"fb_used": fb_used, "done": 0, "failed": 0, "stop": None, "fallback_skipped_cap": 0}
    B = opts["batch_size"]
    batches = [origins[i:i + B] for i in range(0, len(origins), B)]
    log(f"enrich: {len(origins)} distinct pending texts in {len(batches)} batches (<= {B}); workers={opts['workers']} "
        f"jev_rps={opts['jev_rps']} fallback cap {fb_cap} (used {fb_used}); label_config={settings.LABEL_CONFIG}")
    queue = asyncio.Queue()
    for k, b in enumerate(batches):
        queue.put_nowait((k, b))
    t_start = time.time()

    async def do_batch(k, batch):
        batch_key = f"inv{invocation}-b{k}"
        results = await asyncio.gather(*[prov.jev_classify(o["review_id"], o["review_text"], batch_key) for o in batch],
                                       return_exceptions=True)
        stopped = {o["review_id"] for o, r in zip(batch, results) if isinstance(r, BudgetStop)}
        labeled, failed = {}, {}
        for o, r in zip(batch, results):
            if isinstance(r, BudgetStop):
                continue  # never dispatched: stays pending, no attempt counted
            if isinstance(r, Exception):
                failed[o["review_id"]] = f"{type(r).__name__}: {str(r)[:200]}"
            else:
                labeled[o["review_id"]] = from_jev(o["review_text"], r)
        cands = sorted([rid for rid, (lab, det) in labeled.items() if det["route_reasons"]],
                       key=lambda rid: min(labeled[rid][1]["conf"]["topic"], labeled[rid][1]["conf"]["intent"]))
        room = max(0, fb_cap - state["fb_used"])
        chosen, skipped = cands[:room], cands[room:]
        state["fb_used"] += len(chosen)
        state["fallback_skipped_cap"] += len(skipped)
        fb = {}
        if chosen:
            texts = {o["review_id"]: o["review_text"] for o in batch}
            try:
                fb = await _fallback(prov, opts, [(rid, texts[rid]) for rid in chosen], batch_key)
            except BudgetStop as e:
                # stopped before the fallback ran: leave routed texts pending so a resume labels them identically
                log(f"enrich: batch {k} fallback not dispatched ({e}); {len(chosen)} routed texts left pending")
                stopped.update(chosen)
                for rid in chosen:
                    labeled.pop(rid)
                state["fb_used"] -= len(chosen)
            except CallFailed as e:
                log(f"enrich: fallback failed for batch {k} ({e}); keeping Jev labels with needs_review=true")
                for rid in chosen:
                    labeled[rid][1]["fallback_error"] = f"CallFailed: {str(e)[:200]}"
                state["fb_used"] -= len(chosen)
        # ---- atomic commit of this batch ----
        now = time.time()
        db.execute("BEGIN")
        for o in batch:
            rid = o["review_id"]
            if rid in stopped:
                continue
            if rid in failed:
                att = db.execute("SELECT attempts FROM records WHERE review_id=?", (rid,)).fetchone()[0] + 1
                if att >= 2:
                    db.execute("UPDATE records SET status='quarantined', reason=?, attempts=? WHERE review_id=?",
                               (f"enrichment_failed: {failed[rid]}", att, rid))
                else:
                    db.execute("UPDATE records SET attempts=?, reason=? WHERE review_id=?", (att, f"retry_pending: {failed[rid]}", rid))
                continue
            labels, detail = labeled[rid]
            source = "jev"
            if rid in fb:
                labels = apply_fallback(o["review_text"], labels, detail, fb[rid])
                source = "jev+haiku_fallback"
            elif rid in skipped:
                detail["fallback_skipped"] = "fallback cap reached"
            db.execute("""UPDATE records SET status='completed', reason=NULL, attempts=attempts+1, label_config=?, topic=?,
                          subtopic=?, intent=?, sentiment=?, severity=?, entities=?, evidence_quote=?, needs_review=?,
                          cache_source_id=NULL, label_source=?, detail=?, invocation=?, completed_at=? WHERE review_id=?""",
                       (settings.LABEL_CONFIG, labels["topic"], labels["subtopic"], labels["intent"], labels["sentiment"],
                        labels["severity"], json.dumps(labels["entities"], ensure_ascii=False), labels["evidence_quote"],
                        int(labels["needs_review"]), source, json.dumps(detail, ensure_ascii=False), invocation, now, rid))
            for d in db.execute("""SELECT r.review_id FROM reviews r JOIN records c ON c.review_id=r.review_id
                                   WHERE r.text_sha=? AND c.status='pending' AND r.review_id<>?""", (o["text_sha"], rid)).fetchall():
                _copy_to_duplicate(db, rid, d[0], invocation)
        db.execute("COMMIT")
        state["done"] += len(labeled)
        state["failed"] += len(failed)
        if stopped:
            raise BudgetStop(f"cap reached inside batch {k}; {len(labeled)} saved, {len(stopped)} left pending")

    async def worker():
        while not queue.empty() and not state["stop"]:
            k, b = queue.get_nowait()
            try:
                await do_batch(k, b)
            except BudgetStop as e:
                state["stop"] = prov.ledger.stop_reason or "spend_cap"
                log(f"enrich: STOP admitting work - {e}")
                return
            if k % 20 == 0 or queue.empty():
                el = time.time() - t_start
                log(f"enrich: batch {k + 1}/{len(batches)} | texts done {state['done']} failed {state['failed']} | "
                    f"fallback {state['fb_used']}/{fb_cap} | spent ${prov.ledger.spent:.4f} | {state['done'] / max(el, 1e-6):.1f} texts/s")

    await asyncio.gather(*[worker() for _ in range(opts["workers"])])
    # one retry pass for texts that failed once (still pending) unless we were stopped
    if not state["stop"] and only_ids is None:
        retry = [row["review_id"] for row in db.execute("SELECT review_id FROM records WHERE status='pending' AND attempts>=1")]
        if retry:
            log(f"enrich: retry pass for {len(retry)} texts that failed once")
            await run(store, prov, dict(opts, workers=1), invocation, log, only_ids=set(retry))
    return state

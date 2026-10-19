"""Stage 4 - Group (role: group).

Code assigns membership: every completed complaint/cancellation record joins exactly one issue,
ISS-<topic>-<subtopic>, taken from its enrichment label. This mapping is saved (membership.csv) so
ranking never needs a model. Claude Haiku 5.5 then receives a bounded evidence pack (<= 8 quotes per
issue, IDs attached) and proposes a name/description and a coherence flag per issue. Code rejects
any response that adds or drops issue IDs; names are accepted, membership is never changed by the model.
"""
import csv
import hashlib
import json

from . import settings
from .providers import BudgetStop, CallFailed

SYSTEM = (settings.PROMPTS / "group_v1.md").read_text()
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["issues"],
          "properties": {"issues": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["issue_id", "name", "description", "coherent", "note"],
              "properties": {"issue_id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                             "coherent": {"type": "boolean"}, "note": {"type": "string"}}}}}}
EXAMPLES_PER_ISSUE = 8


def issue_id(subtopic):
    topic, sub = subtopic.split(".", 1)
    return f"ISS-{topic}-{sub}"


def build_membership(store):
    rows = store.db.execute("""SELECT review_id, subtopic FROM records WHERE status='completed'
                               AND intent IN ('complaint','cancellation') ORDER BY review_id""").fetchall()
    return [(issue_id(r["subtopic"]), r["review_id"]) for r in rows]


def evidence_pack(store, membership):
    by_issue = {}
    for iid, rid in membership:
        by_issue.setdefault(iid, []).append(rid)
    pack = []
    for iid in sorted(by_issue):
        topic, sub = iid[4:].split("-", 1)
        # deterministic examples: distinct origin texts, most helpful-voted first, then review_id
        ex = store.db.execute(f"""
            SELECT c.review_id, c.evidence_quote, c.severity FROM records c JOIN reviews r USING(review_id)
            WHERE c.status='completed' AND c.cache_source_id IS NULL AND c.intent IN ('complaint','cancellation')
              AND c.subtopic = ? ORDER BY CAST(r.review_likes AS INTEGER) DESC, c.review_id LIMIT ?""",
                              (f"{topic}.{sub}", EXAMPLES_PER_ISSUE)).fetchall()
        pack.append({"issue_id": iid, "topic": topic, "subtopic_definition": settings.SUBTOPICS[f"{topic}.{sub}"],
                     "member_count": len(by_issue[iid]),
                     "examples": [{"review_id": e[0], "quote": e[1][:220], "severity": e[2]} for e in ex]})
    return pack


async def run(store, prov, opts, run_dir, log):
    membership = build_membership(store)
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "membership.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["issue_id", "review_id"])
        w.writerows(membership)
    pack = evidence_pack(store, membership)
    (run_dir / "group_evidence_pack.json").write_text(json.dumps(pack, indent=1, ensure_ascii=False))
    key = "group:" + hashlib.sha256(json.dumps([pack, settings.GROUP_PROMPT_VERSION, settings.HAIKU]).encode()).hexdigest()
    names = store.get_artifact(key)
    expected = {p["issue_id"] for p in pack}
    if names:
        log(f"group: reusing cached issue names for {len(pack)} issues (0 new calls)")
    elif pack:
        def validate(d):
            got = [i["issue_id"] for i in d.get("issues", [])]
            if set(got) != expected or len(got) != len(set(got)):
                return f"issue id set mismatch: extra={sorted(set(got) - expected)[:3]} missing={sorted(expected - set(got))[:3]}"
            return None
        try:
            data, _ = await prov.claude_json(role="group", model=settings.HAIKU, system=SYSTEM,
                                             user="<evidence_pack>\n" + json.dumps(pack, ensure_ascii=False) + "\n</evidence_pack>",
                                             schema=SCHEMA, max_tokens=8000, effort="low", review_ids=[],
                                             prompt_version=settings.GROUP_PROMPT_VERSION, batch_key="group",
                                             validate=validate)
            names = {i["issue_id"]: i for i in data["issues"]}
            store.put_artifact(key, "group", names)
        except (CallFailed, BudgetStop) as e:
            log(f"group: naming call failed ({e}); falling back to subtopic definitions as names")
            names = {}
    issues = []
    for p in pack:
        n = (names or {}).get(p["issue_id"], {})
        issues.append({"issue_id": p["issue_id"], "topic": p["topic"], "subtopic_definition": p["subtopic_definition"],
                       "name": n.get("name") or p["subtopic_definition"], "description": n.get("description", ""),
                       "coherent": n.get("coherent"), "note": n.get("note", ""), "member_count": p["member_count"],
                       "example_review_ids": [e["review_id"] for e in p["examples"]],
                       "name_source": "model:" + settings.HAIKU if n else "code:subtopic_definition"})
    (run_dir / "issues.json").write_text(json.dumps({"membership_file": "membership.csv", "issues": issues}, indent=1,
                                                    ensure_ascii=False))
    log(f"group: {len(membership)} complaint/cancellation records -> {len(issues)} issues (membership.csv, issues.json)")

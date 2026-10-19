"""Offline mock providers for plumbing tests only (PIPELINE_MOCK=1).

Mock runs carry label_config prefix 'MOCK|' and are never used for any reported result.
"""
import json
import re
import uuid
from types import SimpleNamespace

KW = [("billing", r"premium|pay|price|shuffle|charg"), ("playback", r"crash|stop|play|lag|connect"),
      ("access", r"log ?in|account|password"), ("downloads", r"download|offline"), ("catalog", r"song|lyric|search|podcast"),
      ("usability", r"\bads?\b|layout|button|queue")]


class _Hdr(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class MockJev:
    async def system_one(self, state, questions):
        t = state.lower()
        topic = next((k for k, rx in KW if re.search(rx, t)), "other")
        neg = bool(re.search(r"not|n't|bad|worst|hate|stop|crash|annoy", t))
        intent = "complaint" if neg else ("praise" if re.search(r"good|great|love|best|nice", t) else "unclear")
        sev = "3" if neg else "1"
        def choice(opts, pick, conf=0.8):
            p = {o: (conf if o == pick else (1 - conf) / max(1, len(opts) - 1)) for o in opts}
            return SimpleNamespace(type="choice", choice=pick, confidence=conf, probabilities=p)
        answers = {"topic": choice(list(questions["topic"]["criteria"]), topic, 0.3 if topic == "other" and neg else 0.8),
                   "intent": choice(list(questions["intent"]["criteria"]), intent),
                   "severity": choice(list(questions["severity"]["criteria"]), sev),
                   "sentiment": SimpleNamespace(type="score", score=0.5 if neg else 3.5, confidence=0.7)}
        return SimpleNamespace(model="mock-jev", answers=answers, usage=SimpleNamespace(input_tokens=300 + len(state) // 4, output_tokens=6),
                               raw_http_response=SimpleNamespace(headers=_Hdr({"x-typesafe-request-id": f"mock-{uuid.uuid4()}"})))

    async def close(self):
        pass


_TL = {"access": "A", "usability": "U", "playback": "P", "downloads": "D", "catalog": "C", "billing": "B", "other": "O"}
_SUB = {"access": "login_failure", "usability": "ads_frequency", "playback": "crash_freeze", "downloads": "downloads_disappear",
        "catalog": "missing_content", "billing": "free_tier_restrictions", "other": "general_negative"}


def _code_lines(user):
    items = json.loads(user.split("<reviews>\n", 1)[1].rsplit("\n</reviews>", 1)[0])
    out = []
    for it in items:
        t = it["text"].lower()
        topic = next((k for k, rx in KW if re.search(rx, t)), "other")
        neg = bool(re.search(r"not|n't|bad|worst|hate|stop|crash|annoy", t))
        intent = "C" if neg else ("P" if re.search(r"good|great|love|best|nice", t) else "U")
        sub = _SUB[topic] if (neg or topic != "other") else ("general_positive" if intent == "P" else "unrelated_unclear")
        out.append(f"{it['id']} {_TL[topic]} {sub} {intent} {3 if neg else 1} {-1 if neg else 1} 0")
    return "\n".join(out)


def _msg(model, text, user, system):
    sys_len = sum(len(b["text"]) for b in system) if isinstance(system, list) else len(system)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", model=model + "-mock",
                           _request_id=f"mock-{uuid.uuid4()}",
                           usage=SimpleNamespace(input_tokens=len(user) // 4, output_tokens=len(text) // 4,
                                                 cache_read_input_tokens=sys_len // 4, cache_creation_input_tokens=0))


class _Batches:
    """File-backed mock of the Message Batches API so interrupt/resume can be tested across processes."""
    DIR = None

    def _dir(self):
        from . import settings
        d = settings.STATE_DIR / "mock_batches"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def create(self, requests):
        bid = f"msgbatch_mock_{uuid.uuid4().hex[:12]}"
        res = []
        for r in requests:
            p = r["params"]
            text = _code_lines(p["messages"][0]["content"])
            res.append({"custom_id": r["custom_id"], "text": text, "user": p["messages"][0]["content"], "model": p["model"],
                        "system_len": sum(len(b["text"]) for b in p["system"])})
        polls = int(__import__("os").environ.get("MOCK_BATCH_POLLS", "1"))
        (self._dir() / f"{bid}.json").write_text(json.dumps({"results": res, "polls_left": polls}))
        return SimpleNamespace(id=bid, processing_status="in_progress")

    async def retrieve(self, bid):
        f = self._dir() / f"{bid}.json"
        d = json.loads(f.read_text())
        status = "ended" if d["polls_left"] <= 0 else "in_progress"
        d["polls_left"] -= 1
        f.write_text(json.dumps(d))
        n = len(d["results"])
        return SimpleNamespace(id=bid, processing_status=status, request_counts=SimpleNamespace(
            processing=0 if status == "ended" else n, succeeded=n if status == "ended" else 0, errored=0))

    async def results(self, bid):
        d = json.loads((self._dir() / f"{bid}.json").read_text())

        async def gen():
            for r in d["results"]:
                msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=r["text"])], stop_reason="end_turn",
                                      model=r["model"] + "-mock",
                                      usage=SimpleNamespace(input_tokens=len(r["user"]) // 4, output_tokens=len(r["text"]) // 4,
                                                            cache_read_input_tokens=r["system_len"] // 4, cache_creation_input_tokens=0))
                yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=msg))
        return gen()


class _Messages:
    def __init__(self):
        self.batches = _Batches()

    async def create(self, model, system, messages, max_tokens, output_config=None, **kw):
        user = messages[0]["content"]
        if "format" not in (output_config or {}):
            return _msg(model, _code_lines(user), user, system)
        req = output_config["format"]["schema"]["required"]
        if "results" in req:
            items = json.loads(user.split("<reviews>\n", 1)[1].rsplit("\n</reviews>", 1)[0])
            sev_only = "subtopic" not in json.dumps(output_config["format"]["schema"])
            res = []
            for it in items:
                r = {"id": it["id"], "topic": "other", "intent": "complaint", "severity": 2, "sentiment": -0.5}
                if not sev_only:
                    r.update(subtopic="other.general_negative", quote=it["text"][:40], entities=[], needs_review=False)
                res.append(r)
            out = {"results": res}
        elif "issues" in req:
            pack = json.loads(user.split("<evidence_pack>\n", 1)[1].rsplit("\n</evidence_pack>", 1)[0])
            out = {"issues": [{"issue_id": p["issue_id"], "name": "Mock " + p["issue_id"], "description": "mock", "coherent": True, "note": ""}
                              for p in pack]}
        else:
            pack = json.loads(user.split("<evidence_pack>\n", 1)[1].rsplit("\n</evidence_pack>", 1)[0])
            top = pack["issue_ranking_top12"][0]
            q = pack["example_quotes"][0]["review_id"] if pack["example_quotes"] else None
            md = (f"## Recommendation\nMOCK memo. Fund {top['issue_id']} first: {top['complaint_count']['value']} complaints "
                  f"[{top['complaint_count']['claim_id']}]." + (f" Example [R:{q}]." if q else ""))
            out = {"priority_area": "playback", "recommendation": "MOCK", "memo_markdown": md,
                   "cards": [{"title": "Mock", "rationale": "mock", "claim_ids": [top["complaint_count"]["claim_id"]],
                              "review_ids": [q] if q else []}]}
        text = json.dumps(out)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", model=model + "-mock",
                               _request_id=f"mock-{uuid.uuid4()}",
                               usage=SimpleNamespace(input_tokens=len(user) // 4, output_tokens=len(text) // 4,
                                                     cache_read_input_tokens=len(system) // 4, cache_creation_input_tokens=0))


class MockAnthropic:
    def __init__(self):
        self.messages = _Messages()

    async def close(self):
        pass

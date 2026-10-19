"""Model access with the controls the orchestrator relies on.

* Ledger      - one spend ledger shared by all workers. Each call reserves its worst-case cost
                before dispatch; work stops being admitted when spent + reserved + next > budget.
* RateLimiter - global token bucket for Jev requests per second, shared across workers.
* Bounded retries with exponential backoff + jitter for 429/5xx/timeouts/connection errors.
  Every attempt (failed or successful) is written to the calls table with usage and cost.
* Fault injection (PIPELINE_FAULTS env var) for the recovery tests, e.g. "jev:429:2,haiku:badjson:1".
* Model allowlist: any model outside settings.MODEL_ALLOWLIST is refused.
"""
import asyncio
import json
import os
import random
import time
import uuid

from . import settings


class BudgetStop(Exception):
    pass


class CallFailed(Exception):
    pass


class Ledger:
    def __init__(self, store, budget_usd, max_minutes=0):
        self.store = store
        self.budget = budget_usd
        self.spent = store.spent_usd()
        self.spent0 = self.spent
        self.global_spent = global_spent()   # all run databases, including this one, at start
        self.reserved = 0.0
        self.deadline = time.time() + max_minutes * 60 if max_minutes else None
        self.stop_reason = None

    def admit(self, estimate):
        if self.global_spent + (self.spent - self.spent0) + self.reserved + estimate > settings.TOTAL_BUDGET_USD:
            self.stop_reason = "total_budget"
            raise BudgetStop(f"project-wide cap ${settings.TOTAL_BUDGET_USD} would be exceeded")
        if self.deadline and time.time() > self.deadline:
            self.stop_reason = "time_cap"
            raise BudgetStop("time cap reached")
        if self.spent + self.reserved + estimate > self.budget:
            self.stop_reason = "spend_cap"
            raise BudgetStop(f"spend cap: spent {self.spent:.4f} + reserved {self.reserved:.4f} + next {estimate:.4f} > {self.budget}")
        self.reserved += estimate
        return estimate

    def settle(self, reservation, actual):
        self.reserved -= reservation
        self.spent += actual


def global_spent():
    """Logged API spend across every run database (mock databases excluded)."""
    import sqlite3
    total = 0.0
    for p in settings.STATE_DIR.glob("*.sqlite"):
        if p.name.startswith("mock"):
            continue
        con = sqlite3.connect(p)
        try:
            total += con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE COALESCE(label_config,'') NOT LIKE 'MOCK|%' "
                                 "AND COALESCE(model_resolved,'') NOT LIKE '%mock%'").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()
    return total


class RateLimiter:
    def __init__(self, rps):
        self.interval = 1.0 / rps if rps else 0
        self.next_t = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        if not self.interval:
            return
        async with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.interval
        await asyncio.sleep(max(0.0, t - now))


class Faults:
    """Deterministic fault injection used only by the recovery/robustness tests."""

    def __init__(self, spec=None):
        self.left = {}
        for part in filter(None, (spec if spec is not None else os.environ.get("PIPELINE_FAULTS", "")).split(",")):
            target, kind, n = part.split(":")
            self.left[(target, kind)] = int(n)

    def take(self, target, kind):
        if self.left.get((target, kind), 0) > 0:
            self.left[(target, kind)] -= 1
            return True
        return False


def cost_usd(rates, model, usage):
    key = settings.JEV_MODEL if model.startswith("jev") else model
    total = 0.0
    for item in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        total += (usage.get(item) or 0) * rates.get((key, item), 0.0)
    return total


def _transient(exc):
    name = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return (status in (408, 409, 429, 500, 502, 503, 504, 529) or "Timeout" in name or "Connection" in name
            or "RateLimit" in name or "Overloaded" in name or "InternalServer" in name)


async def _backoff(attempt):
    await asyncio.sleep(min(20.0, 0.75 * 2 ** (attempt - 1)) * (0.5 + random.random() / 2))


class Providers:
    def __init__(self, store, ledger, rates, opts, invocation, phase, log):
        self.store, self.ledger, self.rates, self.opts = store, ledger, rates, opts
        self.invocation, self.phase, self.log = invocation, phase, log
        self.faults = Faults()
        self.jev_limiter = RateLimiter(opts["jev_rps"])
        self.jev_sem = asyncio.Semaphore(opts["jev_concurrency"])
        self._jev = None
        self._anth = None
        jq = json.loads((settings.PROMPTS / f"jev_questions_{settings.JEV_QUESTIONS_VERSION.split('-')[-1]}.json").read_text())
        self.jev_questions = jq["questions"]
        self.jev_question_tokens = len(json.dumps(self.jev_questions)) // 3

    # ---- clients (created lazily so offline commands never need keys) ----
    def jev(self):
        if self._jev is None and os.environ.get("PIPELINE_MOCK") == "1":
            from .mock import MockJev
            self._jev = MockJev()
        if self._jev is None:
            from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
            self._jev = AsyncTypeSafeClient(model=settings.JEV_MODEL, retry=RetryPolicy(max_retries=0), timeout=30.0)
        return self._jev

    def anthropic(self):
        if self._anth is None and os.environ.get("PIPELINE_MOCK") == "1":
            from .mock import MockAnthropic
            self._anth = MockAnthropic()
        if self._anth is None:
            import anthropic
            self._anth = anthropic.AsyncAnthropic(max_retries=0, timeout=180.0)
        return self._anth

    async def close(self):
        if self._jev is not None:
            await self._jev.close()
        if self._anth is not None:
            await self._anth.close()

    # ---- Jev: one review per request, several questions per review ----
    async def jev_classify(self, review_id, text, batch_key, role="enrich", phase=None):
        est_in = self.jev_question_tokens + len(text) // 3 + 50
        reservation = self.ledger.admit(cost_usd(self.rates, settings.JEV_MODEL, {"input_tokens": est_in * 1.5}))
        try:
            for attempt in range(1, self.opts["max_attempts"] + 1):
                await self.jev_limiter.wait()
                t0 = time.time()
                call = dict(role=role, phase=phase or self.phase, invocation=self.invocation, provider="typesafe",
                            model=settings.JEV_MODEL, settings={"retry": "pipeline", "timeout_s": 30},
                            prompt_version=settings.JEV_QUESTIONS_VERSION, label_config=settings.LABEL_CONFIG,
                            review_ids=[review_id], attempt=attempt, started_at=t0, batch_key=batch_key)
                try:
                    async with self.jev_sem:
                        if self.faults.take("jev", "429"):
                            raise _InjectedTransient("injected 429 rate limit")
                        resp = await self.jev().system_one(state=text, questions=self.jev_questions)
                    usage = {"input_tokens": resp.usage.input_tokens or 0, "output_tokens": resp.usage.output_tokens or 0}
                    c = cost_usd(self.rates, settings.JEV_MODEL, usage)
                    rid = resp.raw_http_response.headers.get("x-typesafe-request-id") or f"local-{uuid.uuid4()}"
                    self.store.log_call(**call, request_id=rid, model_resolved=resp.model, outcome="succeeded",
                                        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                                        cost_usd=c, latency_ms=int((time.time() - t0) * 1000))
                    self.ledger.settle(0, c)
                    return _jev_payload(resp)
                except Exception as e:  # noqa: BLE001 - every failure is logged, then classified
                    self.store.log_call(**call, request_id=f"failed-{uuid.uuid4()}", outcome="failed",
                                        error=f"{type(e).__name__}: {str(e)[:300]}", input_tokens=0, output_tokens=0,
                                        cost_usd=0.0, latency_ms=int((time.time() - t0) * 1000))
                    if not _transient(e) or attempt == self.opts["max_attempts"]:
                        raise CallFailed(f"jev {review_id}: {type(e).__name__}: {str(e)[:200]}") from e
                    await _backoff(attempt)
        finally:
            self.ledger.settle(reservation, 0)

    # ---- Anthropic structured-output call (fallback / verify / group / memo roles) ----
    async def claude_json(self, *, role, model, system, user, schema, max_tokens, effort, review_ids, prompt_version,
                          batch_key, label_config=None, thinking_disabled=True, validate=None, phase=None):
        if model not in settings.MODEL_ALLOWLIST:
            raise CallFailed(f"model {model} not in allowlist")
        est_in = (len(system) + len(user)) // 3
        worst = cost_usd(self.rates, model, {"input_tokens": est_in, "output_tokens": max_tokens})
        invalid_retries = 0
        attempt = 0
        while True:
            attempt += 1
            reservation = self.ledger.admit(worst)
            t0 = time.time()
            call = dict(role=role, phase=phase or (self.phase if role == "enrich" else "initial"), invocation=self.invocation,
                        provider="anthropic", model=model,
                        settings={"effort": effort, "thinking": "disabled" if thinking_disabled else "adaptive",
                                  "max_tokens": max_tokens},
                        prompt_version=prompt_version, label_config=label_config or "", review_ids=review_ids,
                        attempt=attempt, started_at=t0, batch_key=batch_key)
            kwargs = dict(model=model, max_tokens=max_tokens,
                          system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                          messages=[{"role": "user", "content": user}],
                          output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
            if thinking_disabled:
                kwargs["thinking"] = {"type": "disabled"}
            try:
                if self.faults.take(role, "500"):
                    raise _InjectedTransient("injected 500 server error")
                resp = await self.anthropic().messages.create(**kwargs)
                u = resp.usage
                usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                         "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                         "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
                c = cost_usd(self.rates, model, usage)
                self.ledger.settle(reservation, c)
                base = dict(call, request_id=resp._request_id or f"local-{uuid.uuid4()}", model_resolved=resp.model,
                            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                            cache_read_tokens=usage["cache_read_tokens"], cache_write_tokens=usage["cache_write_tokens"],
                            cost_usd=c, latency_ms=int((time.time() - t0) * 1000))
                problem = None
                if resp.stop_reason in ("refusal", "max_tokens"):
                    problem = f"stop_reason={resp.stop_reason}"
                    data = None
                else:
                    text = next((b.text for b in resp.content if b.type == "text"), "")
                    if self.faults.take(role, "badjson"):
                        text = text[: len(text) // 2]  # simulate truncated/malformed output
                    try:
                        data = json.loads(text)
                        problem = validate(data) if validate else None
                    except json.JSONDecodeError as e:
                        data, problem = None, f"invalid JSON: {e}"
                if problem is None:
                    self.store.log_call(**base, outcome="succeeded")
                    return data, usage
                self.store.log_call(**base, outcome="failed", error=f"invalid_output: {problem}"[:400])
                invalid_retries += 1
                if invalid_retries > 1:                      # retry invalid output once, then give up
                    raise CallFailed(f"{role}: invalid output after retry: {problem}")
            except (CallFailed, BudgetStop):
                raise
            except Exception as e:  # noqa: BLE001
                self.ledger.settle(reservation, 0)
                self.store.log_call(**call, request_id=f"failed-{uuid.uuid4()}", outcome="failed",
                                    error=f"{type(e).__name__}: {str(e)[:300]}", input_tokens=0, output_tokens=0,
                                    cost_usd=0.0, latency_ms=int((time.time() - t0) * 1000))
                if not _transient(e) or attempt >= self.opts["max_attempts"]:
                    raise CallFailed(f"{role}: {type(e).__name__}: {str(e)[:200]}") from e
                await _backoff(attempt)


class _InjectedTransient(Exception):
    status_code = 429


def _jev_payload(resp):
    out = {"model": resp.model}
    for name, a in resp.answers.items():
        if a.type == "choice":
            out[name] = {"choice": a.choice, "confidence": a.confidence, "probabilities": dict(a.probabilities)}
        elif a.type == "score":
            out[name] = {"score": a.score, "confidence": a.confidence}
        elif a.type == "noul":
            out[name] = {"noul": a.noul}
    return out

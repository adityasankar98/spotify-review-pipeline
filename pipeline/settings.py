"""Central configuration: paths, versions, models, label_config and default controls."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "labels"
PROMPTS = ROOT / "prompts"
STATE_DIR = ROOT / "data" / "state"
RUNS_DIR = ROOT / "runs"

SOURCE_FIELDS = ("review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp")
TOPICS = ("access", "usability", "playback", "downloads", "catalog", "billing", "support", "other")
INTENTS = ("cancellation", "complaint", "request", "praise", "unclear")  # precedence order

TAXONOMY = json.loads((LABELS / "taxonomy.json").read_text())
SUBTOPICS = json.loads((LABELS / "subtopics.json").read_text())["subtopics"]

# ---- models (allowlist) ----
JEV_MODEL = "jev-latest"            # alias; the resolved version (e.g. jev-1.13.0) is logged per call
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
MODEL_ALLOWLIST = {JEV_MODEL, HAIKU, SONNET}

# ---- prompt / schema versions ----
ENRICH_PROMPT_VERSION = "enrich-codes-v1"  # Haiku primary enricher, coded one-line-per-review output
JEV_QUESTIONS_VERSION = "jev-q-v2"  # Jev questions, now used by the independent verifier
FALLBACK_PROMPT_VERSION = "fallback-v1"  # superseded JSON prompt; kept for the measured format comparison
VERIFY_PROMPT_VERSION = "verify-v1"
GROUP_PROMPT_VERSION = "group-v1"
MEMO_PROMPT_VERSION = "memo-v1"
SCHEMA_VERSION = "schema-v1"
EXTRACT_VERSION = "extract-v2"  # quote + entities + code subtopic rules


def _prompt_digest(*names):
    h = hashlib.sha256()
    for n in names:
        h.update((PROMPTS / n).read_bytes())
    return h.hexdigest()[:8]


# label_config names everything that determines a completed classification: model, effort/thinking,
# the enrichment prompt, the deterministic quote/entity extractor and the record schema.
# Exact-text cache reuse is only valid when this whole string is identical. The transport
# (Batch API vs. synchronous Messages API) changes price and turnaround, not the request, so it is not part of it.
ENRICH_MAX_TOKENS = 2000          # output cap per enrichment request (<=50 reviews, ~15-20 tokens each)
LABEL_CONFIG = (f"enrich-v2|{HAIKU}:effort=low,thinking=disabled,max_tokens={ENRICH_MAX_TOKENS}:{ENRICH_PROMPT_VERSION}"
                f"|{EXTRACT_VERSION}|{SCHEMA_VERSION}|prompts={_prompt_digest('enrich_codes_v1.md')}")

import os as _os
if _os.environ.get("PIPELINE_MOCK") == "1":
    LABEL_CONFIG = "MOCK|" + LABEL_CONFIG  # mock runs can never be confused with real classifications

QUOTE_FULLTEXT_MAX_CHARS = 280   # short reviews use the full text as evidence quote

# ---- project-wide spend caps: every run database's logged spend counts against them ----
# Out-of-pocket limit is $10 ($5 prepaid Anthropic + $5 prepaid TypeSafe, auto-reload off). The code stops
# earlier, leaving room for estimate error and delayed billing.
PROVIDER_CAPS_USD = {"anthropic": 4.50, "typesafe": 4.50}
TOTAL_BUDGET_USD = sum(PROVIDER_CAPS_USD.values())

# ---- default run controls (overridable on the CLI) ----
DEFAULTS = {
    "batch_size": 50,             # max reviews per enrichment request (contract limit 50)
    "transport": "batch",         # batch = Message Batches API (50% price, async); sync = Messages API
    "batch_requests": 200,        # enrichment requests per submitted Message Batch (200 x 50 = 10,000 reviews)
    "poll_seconds": 20,           # Message Batch status polling interval
    "workers": 1,                 # concurrent in-flight Message Batches (sync transport: concurrent requests)
    "jev_rps": 8.0,               # global Jev request rate limit
    "jev_concurrency": 16,        # max in-flight Jev requests across workers
    "budget_usd": 2.0,            # per-run spend cap: spent + reserved + next reservation must stay below this
    "max_attempts": 4,            # transient-error attempts per call (1 + 3 retries, backoff + jitter)
    "max_minutes": 0,             # wall-clock cap; 0 = none
    "verify_sample": 200,
    "verify_seed": "verify-seed-v1",
}


def load_rates(path=None):
    import csv
    path = Path(path or ROOT / "cost" / "rates.csv")
    rates = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            rates[(r["model"], r["item"])] = float(r["usd_per_unit"]) / float(r["unit_size"])
    return rates

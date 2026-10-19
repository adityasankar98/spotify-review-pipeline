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
JEV_QUESTIONS_VERSION = "jev-q-v2"  # compact set for the $10 budget (v1 kept for reference)
FALLBACK_PROMPT_VERSION = "fallback-v1"
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


# label_config names everything that determines a completed classification: the Jev questions,
# the deterministic quote/entity extractor, the Haiku fallback prompt and its routing thresholds.
# Exact-text cache reuse is only valid when this whole string is identical.
LABEL_CONFIG = (f"enrich-v1|{JEV_MODEL}:{JEV_QUESTIONS_VERSION}|{HAIKU}:effort=low,thinking=disabled:{FALLBACK_PROMPT_VERSION}"
                f"|{EXTRACT_VERSION}|{SCHEMA_VERSION}|prompts={_prompt_digest('jev_questions_v2.json', 'enrich_fallback_v1.md')}")

import os as _os
if _os.environ.get("PIPELINE_MOCK") == "1":
    LABEL_CONFIG = "MOCK|" + LABEL_CONFIG  # mock runs can never be confused with real classifications

# ---- routing thresholds (chosen on development data, never on golden labels) ----
FALLBACK_TOPIC_CONF = 0.35       # Jev topic confidence below this -> fallback candidate
FALLBACK_INTENT_CONF = 0.35
QUOTE_FULLTEXT_MAX_CHARS = 280   # short reviews use the full text as evidence quote

# ---- project-wide spend cap: every run database's logged spend counts against it ----
TOTAL_BUDGET_USD = 10.0

# ---- default run controls (overridable on the CLI) ----
DEFAULTS = {
    "batch_size": 50,             # max reviews per enrichment batch / per Haiku request (contract limit 50)
    "workers": 1,                 # concurrent batches
    "jev_rps": 8.0,               # global Jev request rate limit
    "jev_concurrency": 16,        # max in-flight Jev requests across workers
    "budget_usd": 2.0,            # per-run spend cap: spent + reserved + next reservation must stay below this
    "max_fallback_fraction": 0.03,  # cap on texts routed to the Haiku fallback
    "fallback_max_tokens": 6000,  # output-token cap per fallback request (<=50 reviews)
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

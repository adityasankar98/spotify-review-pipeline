"""Local golden-set labeling UI (stdlib only, binds to 127.0.0.1).

    python3 tools/label_server.py            # then open http://127.0.0.1:8765

Labels are saved to evals/golden/golden_50_human.csv after every change.
These human labels are evaluation-only and are never read by any model stage.
"""
import csv
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data/raw/golden_50_to_label.csv"
OUT = ROOT / "evals/golden/golden_50_human.csv"
TAX = ROOT / "labels/taxonomy.json"
PAGE = ROOT / "tools/label_ui.html"
SOURCE_FIELDS = ["review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp"]
LABEL_FIELDS = ["topic", "intent", "sentiment", "severity", "entities", "evidence_quote", "needs_review",
                "ambiguous", "alt_topics", "alt_intents", "alt_severities", "notes"]


def load_rows():
    with SRC.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    saved = {}
    if OUT.exists():
        with OUT.open(encoding="utf-8", newline="") as f:
            saved = {r["review_id"]: r for r in csv.DictReader(f)}
    for r in rows:
        for k in LABEL_FIELDS:
            r[k] = saved.get(r["review_id"], {}).get(k, r.get(k, "")) or ""
    return rows


def save_rows(rows):
    tmp = OUT.with_suffix(".tmp")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SOURCE_FIELDS + LABEL_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(OUT)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/data":
            self._send(200, json.dumps({"rows": load_rows(), "taxonomy": json.loads(TAX.read_text())}))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if self.path != "/api/save":
            return self._send(404, "{}")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        rows = load_rows()
        by_id = {r["review_id"]: r for r in rows}
        rec = by_id.get(body.get("review_id"))
        if not rec:
            return self._send(400, json.dumps({"error": "unknown id"}))
        for k in LABEL_FIELDS:
            if k in body:
                rec[k] = str(body[k])
        q = rec["evidence_quote"]
        if q and q not in rec["review_text"]:
            return self._send(400, json.dumps({"error": "evidence_quote must be an exact substring of the review"}))
        save_rows(rows)
        done = sum(1 for r in rows if r["topic"] and r["intent"] and r["severity"] and r["sentiment"])
        self._send(200, json.dumps({"ok": True, "done": done}))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Labeling UI on http://127.0.0.1:{port}  ->  saving to {OUT}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
